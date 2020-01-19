"""Scalene: a high-performance, high-precision CPU *and* memory profiler for Python.

    Scalene uses interrupt-driven sampling for CPU profiling. For memory
    profiling, it uses a similar mechanism but with interrupts generated
    by a "sampling memory allocator" that produces signals everytime the
    heap grows or shrinks by a certain amount. See libscalene.cpp for
    details (sampling logic is in include/sampleheap.hpp).

    by Emery Berger
    https://emeryberger.com

    usage: # for CPU profiling only
            python -m Scalene test/testme.py
            # for CPU and memory profiling (Mac OS X)
            DYLD_INSERT_LIBRARIES=$PWD/libscalene.dylib PYTHONMALLOC=malloc python -m scalene test/testme.py
            # for CPU and memory profiling (Linux)
            LD_PRELOAD=$PWD/libscalene.so PYTHONMALLOC=malloc python -m scalene test/testme.py

"""

import random
import sys
import atexit
import signal
import math
from collections import defaultdict
import time
from pathlib import Path
import os
import traceback
import argparse
from contextlib import contextmanager
from functools import lru_cache
from textwrap import dedent

the_globals = {
    '__name__': '__main__',
    '__doc__': None,
    '__package__': None,
    '__loader__': globals()['__loader__'],
    '__spec__': None,
    '__annotations__': {},
    '__builtins__': globals()['__builtins__'],
    '__file__': None,
    '__cached__': None,
}

assert sys.version_info[0] == 3 and sys.version_info[1] >= 5, "Scalene requires Python version 3.5 or above."

# Scalene currently only supports Unix-like operating systems; in particular, Linux and Mac OS X.
if sys.platform == 'win32':
    print("Scalene currently does not support Windows, but works on Linux and Mac OS X.")
    sys.exit(-1)

class Scalene():
    """The Scalene profiler itself."""
    # Statistics counters.
    cpu_samples_python            = defaultdict(lambda: defaultdict(int))  # CPU    samples for each location in the program
                                                                           #        spent in the interpreter
    cpu_samples_c                 = defaultdict(lambda: defaultdict(int))  # CPU    samples for each location in the program
                                                                           #        spent in C / libraries / system calls
    memory_free_samples           = defaultdict(lambda: defaultdict(int))  # malloc samples for each location in the program
    memory_malloc_samples         = defaultdict(lambda: defaultdict(int))  # free   "       "   "    "        "   "  "
    total_cpu_samples             = 0              # how many CPU    samples have been collected.
    total_memory_free_samples     = 0              # "   "    malloc "       "    "    "
    total_memory_malloc_samples   = 0              # "   "    free   "       "    "    "
    mean_signal_interval          = 0.01           # mean seconds between interrupts for CPU sampling.
    last_signal_interval          = 0.01           # last num seconds between interrupts for CPU sampling.
    elapsed_time                  = 0              # total time spent in program being profiled.
    malloc_sampling_rate          = 256 * 1024 * 1024  # we get signals after this many bytes are allocated.
                                                       # NB: MUST BE IN SYNC WITH include/sampleheap.hpp!
    free_sampling_rate            = 256 * 1024 * 1024  # as above, for frees.

    # The specific signals we use. Malloc and free signals are generated by include/sampleheap.hpp.
    # cpu_timer_signal = signal.ITIMER_REAL
    cpu_timer_signal = signal.ITIMER_VIRTUAL

    if cpu_timer_signal == signal.ITIMER_REAL:
        cpu_signal  = signal.SIGALRM
    elif cpu_timer_signal == signal.ITIMER_VIRTUAL:
        cpu_signal = signal.SIGVTALRM
    elif cpu_timer_signal == signal.ITIMER_PROF:
        # NOT SUPPORTED
        assert False, "ITIMER_PROF is not currently supported."

    malloc_signal = signal.SIGPROF
    free_signal   = signal.SIGXCPU

    # Program-specific information.
    program_being_profiled = ""          # the name of the program being profiled.
    program_path           = ""          # the path "  "   "       "     "

    @staticmethod
    def gettime():
        """High-precision timer of time spent running in or on behalf of this process."""
        return time.process_time()

    def __init__(self, program_being_profiled):
        # Register the exit handler to run when the program terminates or we quit.
        atexit.register(Scalene.exit_handler)
        # Store relevant names (program, path).
        Scalene.program_being_profiled = os.path.abspath(program_being_profiled)
        Scalene.program_path = os.path.dirname(Scalene.program_being_profiled)
        # Set up the signal handler to handle periodic timer interrupts (for CPU).
        signal.signal(Scalene.cpu_signal, self.cpu_signal_handler)
        # Set up the signal handler to handle malloc interrupts (for memory allocations).
        signal.signal(Scalene.malloc_signal, self.malloc_signal_handler)
        signal.signal(Scalene.free_signal, self.free_signal_handler)
        # Turn on the CPU profiling timer to run every signal_interval seconds.
        signal.setitimer(Scalene.cpu_timer_signal, self.mean_signal_interval, self.mean_signal_interval)
        Scalene.last_signal_time = Scalene.gettime()


    @staticmethod
    def cpu_signal_handler(_, frame):
        """Handle interrupts for CPU profiling."""
        # Record how long it has been since we received a timer
        # before.  See the logic below.
        now = Scalene.gettime()
        elapsed_since_last_signal = now - Scalene.last_signal_time
        fname = frame.f_code.co_filename
        # Record samples only for files we care about.
        if (len(fname)) == 0:
            # 'eval/compile' gives no f_code.co_filename.
            # We have to look back into the outer frame in order to check the co_filename.
            fname = frame.f_back.f_code.co_filename
        if not Scalene.should_trace(fname):
            Scalene.last_signal_time = Scalene.gettime()
            Scalene.last_signal_interval = random.uniform(Scalene.mean_signal_interval / 2, Scalene.mean_signal_interval * 3 / 2)
            signal.setitimer(Scalene.cpu_timer_signal, Scalene.last_signal_interval, Scalene.last_signal_interval)
            return
        # Here we take advantage of an apparent limitation of Python:
        # it only delivers signals after the interpreter has given up
        # control. This seems to mean that sampling is limited to code
        # running purely in the interpreter, and in fact, that was a limitation
        # of the first version of Scalene.
        #
        # (cf. https://docs.python.org/3.9/library/signal.html#execution-of-python-signal-handlers)
        #
        # However: lemons -> lemonade: this "problem" is in fact
        # an effective way to separate out time spent in
        # Python vs. time spent in native code "for free"!  If we get
        # the signal immediately, we must be running in the
        # interpreter. On the other hand, if it was delayed, that means
        # we are running code OUTSIDE the interpreter, e.g.,
        # native code (be it inside of Python or in a library). We
        # account for this time by tracking the elapsed (process) time
        # and compare it to the interval, and add any computed delay
        # (as if it were sampled) to the C counter.
        c_time = elapsed_since_last_signal - Scalene.last_signal_interval
        Scalene.cpu_samples_python[fname][frame.f_lineno] += 1
        Scalene.cpu_samples_c[fname][frame.f_lineno] += c_time / Scalene.last_signal_interval
        Scalene.total_cpu_samples += elapsed_since_last_signal / Scalene.last_signal_interval
        Scalene.last_signal_interval = random.uniform(Scalene.mean_signal_interval / 2, Scalene.mean_signal_interval * 3 / 2)
        signal.setitimer(Scalene.cpu_timer_signal, Scalene.last_signal_interval, Scalene.last_signal_interval)
        Scalene.last_signal_time = Scalene.gettime()
        return

    @staticmethod
    def malloc_signal_handler(_, frame):
        """Handle interrupts for memory profiling (mallocs)."""
        fname = frame.f_code.co_filename
        # Record samples only for files we care about.
        if not Scalene.should_trace(fname):
            return
        Scalene.memory_malloc_samples[fname][frame.f_lineno] += 1
        Scalene.total_memory_malloc_samples += 1
        return

    @staticmethod
    def free_signal_handler(_, frame):
        """Handle interrupts for memory profiling (frees)."""
        fname = frame.f_code.co_filename
        # Record samples only for files we care about.
        if not Scalene.should_trace(fname):
            return
        Scalene.memory_free_samples[fname][frame.f_lineno] += 1
        Scalene.total_memory_free_samples += 1
        return

    @staticmethod
    @lru_cache(128)
    def should_trace(filename):
        """Return true if the filename is one we should trace."""
        # Profile anything in the program's directory or a child directory,
        # but nothing else.
        if filename[0] == '<':
            # Don't profile Python internals.
            return False
        if 'scalene.py' in filename:
            # Don't profile the profiler.
            return False
        filename = os.path.abspath(filename)
        return Scalene.program_path in filename

    @staticmethod
    def start():
        """Initiate profiling."""
        Scalene.elapsed_time = Scalene.gettime()

    @staticmethod
    def stop():
        """Complete profiling."""
        Scalene.disable_signals()
        Scalene.elapsed_time = Scalene.gettime() - Scalene.elapsed_time

    @staticmethod
    @contextmanager
    def file_or_stdout(file_name):
        """Returns a file handle for writing; if no argument is passed, returns stdout."""
        # from https://stackoverflow.com/questions/9836370/fallback-to-stdout-if-no-file-name-provided
        if file_name is None:
            yield sys.stdout
        else:
            with open(file_name, 'w') as out_file:
                yield out_file

    @staticmethod
    def output_profiles(output_file):
        """Write the profile out (currently to stdout)."""
        # If I have at least one memory sample, then we are profiling memory.
        did_sample_memory = (Scalene.total_memory_free_samples + Scalene.total_memory_malloc_samples) > 1
        # Collect all instrumented filenames.
        all_instrumented_files = list(set(list(Scalene.cpu_samples_python.keys()) + list(Scalene.memory_free_samples.keys()) + list(Scalene.memory_malloc_samples.keys())))
        with Scalene.file_or_stdout(output_file) as out:
            for fname in sorted(all_instrumented_files):

                this_cpu_samples = sum(Scalene.cpu_samples_c[fname].values()) + sum(Scalene.cpu_samples_python[fname].values())

                try:
                    percent_cpu_time = 100 * this_cpu_samples / Scalene.total_cpu_samples
                except ZeroDivisionError:
                    percent_cpu_time = 0

                # percent_cpu_time = 100 * this_cpu_samples * Scalene.mean_signal_interval / Scalene.elapsed_time
                print("%s: %% of CPU time = %6.2f%% out of %6.2fs." % (fname, percent_cpu_time, Scalene.elapsed_time), file=out)
                print("  \t | %9s | %9s | %s %s " % ('CPU %', 'CPU %', 'Memory (MB) |' if did_sample_memory else '', 'Memory (MB) |' if did_sample_memory else ''), file=out)
                print("  Line\t | %9s | %9s | %s%s [%s]" % ('(Python)', '(C)', '     Growth |' if did_sample_memory else '', '       Usage |' if did_sample_memory else '', fname), file=out)
                print("-" * 80, file=out)

                with open(fname, 'r') as source_file:
                    for line_no, line in enumerate(source_file, 1):
                        line = line.rstrip() # Strip newline
                        # Prepare output values.
                        n_cpu_samples_c = Scalene.cpu_samples_c[fname][line_no]
                        # Correct for negative CPU sample counts.
                        # This can happen because of floating point inaccuracies, since we perform subtraction to compute it.
                        if n_cpu_samples_c < 0:
                            n_cpu_samples_c = 0
                        n_cpu_samples_python = Scalene.cpu_samples_python[fname][line_no]
                        # Compute percentages of CPU time.
                        if Scalene.total_cpu_samples != 0:
                            n_cpu_percent_c = n_cpu_samples_c * 100 / Scalene.total_cpu_samples
                            n_cpu_percent_python = n_cpu_samples_python * 100 / Scalene.total_cpu_samples
                        else:
                            n_cpu_percent_c = 0
                            n_cpu_percent_python = 0
                        # Now, memory stats.
                        n_free_mb = (Scalene.memory_free_samples[fname][line_no] * Scalene.malloc_sampling_rate) / (1024 * 1024)
                        n_malloc_mb = (Scalene.memory_malloc_samples[fname][line_no] * Scalene.malloc_sampling_rate) / (1024 * 1024)
                        n_growth_mb = n_malloc_mb - n_free_mb
                        n_usage_mb = n_malloc_mb + n_free_mb

                        # Finally, print results.
                        n_cpu_percent_c_str = "" if n_cpu_percent_c == 0 else '%6.2f%%' % n_cpu_percent_c
                        n_cpu_percent_python_str = "" if n_cpu_percent_python == 0 else '%6.2f%%' % n_cpu_percent_python
                        n_growth_mb_str  = "" if n_growth_mb == 0 else '%9.2f' % n_growth_mb
                        n_usage_mb_str  = "" if n_usage_mb == 0 else '%9.2f' % n_usage_mb
                        if did_sample_memory:
                            print("%6d\t | %9s | %9s | %11s | %11s | %s" %
                                  (line_no, n_cpu_percent_python_str, n_cpu_percent_c_str, n_growth_mb_str, n_usage_mb_str, line), file=out)
                        else:
                            print("%6d\t | %9s | %9s | %s" %
                                  (line_no, n_cpu_percent_python_str, n_cpu_percent_c_str, line), file=out)
                    print("", file=out)


    @staticmethod
    def disable_signals():
        """Turn off the profiling signals."""
        try:
            signal.signal(Scalene.cpu_timer_signal, signal.SIG_IGN)
        except Exception as ex:
            pass
        signal.signal(Scalene.malloc_signal, signal.SIG_IGN)
        signal.signal(Scalene.free_signal, signal.SIG_IGN)
        signal.setitimer(Scalene.cpu_timer_signal, 0)

    @staticmethod
    def exit_handler():
        """When we exit, disable all signals."""
        Scalene.disable_signals()

    @staticmethod
    def main():
        """Invokes the profiler from the command-line."""
        usage = dedent("""Scalene: a high-precision CPU and memory profiler.
            https://github.com/emeryberger/Scalene

                for CPU profiling only:
            % python -m scalene yourprogram.py
                for CPU and memory profiling (Mac OS X):
            % DYLD_INSERT_LIBRARIES=$PWD/libscalene.dylib PYTHONMALLOC=malloc python -m scalene yourprogram.py
                for CPU and memory profiling (Linux):
            % LD_PRELOAD=$PWD/libscalene.so PYTHONMALLOC=malloc python -m scalene yourprogram.py
            """)
        parser = argparse.ArgumentParser(prog='scalene', description=usage, formatter_class=argparse.RawTextHelpFormatter)
        parser.add_argument('prog', type=str, help='program to be profiled')
        parser.add_argument('-o', '--outfile', type=str, default=None, help='file to hold profiler output (default: stdout)')
        # Parse out all Scalene arguments and jam the remaining ones into argv.
        # See https://stackoverflow.com/questions/35733262/is-there-any-way-to-instruct-argparse-python-2-7-to-remove-found-arguments-fro
        args, left = parser.parse_known_args()
        sys.argv = sys.argv[:1]+left
        try:
            with open(args.prog, 'rb') as prog_being_profiled:
                original_path = os.getcwd()
                # Read in the code and compile it.
                code = compile(prog_being_profiled.read(), args.prog, "exec")
                # Push the program's path.
                program_path = os.path.dirname(os.path.abspath(args.prog))
                sys.path.insert(0, program_path)
                os.chdir(program_path)
                # Set the file being executed.
                the_globals['__file__'] = args.prog
                # Start the profiler.
                profiler = Scalene(os.path.join(program_path, os.path.basename(args.prog)))
                try:
                    profiler.start()
                    # Run the code being profiled.
                    exec(code, the_globals)
                    profiler.stop()
                    # Go back home.
                    os.chdir(original_path)
                    # If we've collected any samples, dump them.
                    if profiler.total_cpu_samples > 0 or profiler.total_memory_malloc_samples > 0 or profiler.total_memory_free_samples > 0:
                        profiler.output_profiles(args.outfile)
                    else:
                        print("Scalene: Program did not run for long enough to profile.")
                except Exception as ex:
                    template = "Scalene: An exception of type {0} occurred. Arguments:\n{1!r}"
                    message = template.format(type(ex).__name__, ex.args)
                    print(message)
                    print(traceback.format_exc())
        except (FileNotFoundError, IOError):
            print("Scalene: could not find input file.")

Scalene.main()
