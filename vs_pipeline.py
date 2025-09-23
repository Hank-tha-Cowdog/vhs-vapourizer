# ============================================================================
# CONFIGURATION - UPDATE THESE PATHS ONLY
# ============================================================================
INPUT_FILE_PATH = r"input\path\to\your\source\file.mkv"
OUTPUT_DIRECTORY_PATH = r"output\path\to\final\file.mkv"

# ============================================================================
# SIMPLE TEST/FULL MODE CONTROL - ONLY CHANGE THESE TWO LINES
# ============================================================================
TEST_MODE = True          # ← CHANGE THIS: True = test mode, False = full processing  
TEST_FRAME_COUNT = 1000    # ← CHANGE THIS: Number of frames to process in test mode
USE_PRORES_OUTPUT = False # ← CHANGE THIS: True = ProRes 422 HQ, False = FFV1 (default)

# ============================================================================
# DEVELOPER TOOLS - DEBUGGING AND PERFORMANCE OPTIONS  
# ============================================================================
DEBUG_MODE = True
ENABLE_PERFORMANCE_PROFILING = True
ENABLE_GPU_MONITORING = True
ENABLE_DETAILED_TIMING = True
PROCESS_DURATION = None  # Advanced: Set to seconds to limit processing time, or None for full file

# ============================================================================
# Y4M WORKFLOW NOTE:
# ============================================================================
# This pipeline now uses Y4M (YUV4MPEG) piping exclusively to avoid pixel format
# mismatches. Y4M automatically handles dimensions, frame rate, and pixel format
# metadata, eliminating the garbled output issues from raw video piping.
# ============================================================================

# Import all required modules
import os
import sys
import subprocess
import time
import re
from tqdm import tqdm
import signal
import threading
import datetime
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import io
import cProfile
import pstats
import queue

# Add Rich library for better terminal UI
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn, SpinnerColumn
    from rich.table import Table
    from rich.live import Live
    from rich.layout import Layout
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Consider installing 'rich' for better terminal UI: pip install rich")

# Add psutil for better process management (optional)
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Consider installing 'psutil' for better process cleanup: pip install psutil")

# ============================================================================
# GLOBAL VARIABLES
# ============================================================================
class AppContext:
    def __init__(self):
        self.process = None
        self.process_manager = None
        self.current_frame = 0
        self.frame_count = 0
        self.pbar = None
        self.cmd = None
        self.venv_env = None
        self.has_nvidia_gpu = False
        self.start_time = None
        self.profiler = None
        self.run_dir = None
        self.log_file = None
        self.input_file = None
        self.output_dir = None
        self.output_file = None
        self.index_status = None
        self.vapoursynth_started = False
        self.processing_task_created = False
        self.last_frame_time = None
        self.last_frame_count = 0
        self.processing_start_time = None

context = AppContext()
processing_times = {}

# ============================================================================
# FUNCTION DEFINITIONS - ALL FUNCTIONS DEFINED FIRST
# ============================================================================

def record_timing(stage_name):
    """Record time taken for a specific processing stage"""
    if ENABLE_DETAILED_TIMING:
        now = time.time()
        if 'last_time' in processing_times:
            elapsed = now - processing_times['last_time']
            processing_times[stage_name] = elapsed
            print(f"Time for {stage_name}: {elapsed:.2f} seconds")
        processing_times['last_time'] = now

def log_message(message, print_to_console=True, force_console=False):
    """Log message to file and optionally to console"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    
    # Log to file if available
    if context.log_file:
        try:
            with open(context.log_file, 'a', encoding='utf-8') as f:
                f.write(full_message + "\n")
                f.flush()  # Ensure immediate write
        except Exception as e:
            # Only show logging error once to avoid spam
            if not hasattr(context, '_log_error_shown'):
                print(f"!!! LOGGING ERROR: {str(e)}")
                context._log_error_shown = True
    
    # Console output
    if print_to_console:
        if force_console or context.pbar is None:
            # Direct console output when no progress bar or forced
            print(full_message)
        # When progress bar is active, let Rich handle all output
        # Don't interfere with the progress display

def debug_log(message):
    """Log message only when in debug mode"""
    if DEBUG_MODE:
        log_message(f"[DEBUG] {message}")
        
        debug_file = os.path.join(context.run_dir, "debug_log.txt")
        with open(debug_file, 'a') as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

def save_performance_profile():
    """Save the performance profile data to a file"""
    if not ENABLE_PERFORMANCE_PROFILING or context.profiler is None:
        log_message("Performance profiling was not enabled or profiler is None")
        return
        
    try:
        context.profiler.disable()
        
        profile_path = os.path.join(context.run_dir, "performance_profile.txt")
        log_message(f"Saving performance profile to {profile_path}")
        
        s = io.StringIO()
        
        try:
            import pstats
            ps = pstats.Stats(context.profiler, stream=s).sort_stats('cumulative')
            ps.print_stats(50)
        except ImportError:
            log_message("Error: pstats module not available, using basic profiling output")
            s.write("pstats module not available for detailed profiling output\n")
            s.write(str(context.profiler))
        
        with open(profile_path, 'w') as f:
            f.write("VapourSynth Processing Pipeline Performance Profile\n")
            f.write("=================================================\n\n")
            f.write(s.getvalue())
            
            if processing_times and len(processing_times) > 1:
                f.write("\n\nStage Timing Information:\n")
                f.write("========================\n\n")
                for stage, duration in processing_times.items():
                    if stage != 'last_time':
                        f.write(f"{stage}: {duration:.2f} seconds\n")
                        
            total_time = time.time() - context.start_time
            f.write(f"\nTotal processing time: {total_time:.2f} seconds ({total_time/60:.2f} minutes)\n")
            if total_time > 0 and context.frame_count and context.frame_count > 0:
                f.write(f"Average processing speed: {context.frame_count/total_time:.2f} frames per second\n")
            
        log_message(f"Performance profile saved to {profile_path}")
        return True
    except Exception as e:
        log_message(f"Error saving performance profile: {str(e)}")
        import traceback
        log_message(f"Traceback: {traceback.format_exc()}")
        return False

def signal_handler(sig, frame):
    """Handle interrupt signals gracefully with improved cleanup"""
    log_message(f"Received signal {sig}, shutting down gracefully...", force_console=True)
    
    # Stop all monitoring threads first
    if hasattr(context, 'process_manager') and context.process_manager:
        try:
            context.process_manager.stop()
        except Exception as e:
            log_message(f"Error stopping process manager: {str(e)}", force_console=True)
    
    # Gracefully terminate the main process
    if context.process is not None:
        try:
            log_message("Terminating VapourSynth/FFmpeg processes...", force_console=True)
            
            # Send SIGTERM to allow FFmpeg to finish current frame
            context.process.terminate()
            
            # Wait longer for graceful shutdown (FFmpeg needs time to finalize)
            try:
                context.process.wait(timeout=15)  # Increased timeout
                log_message("Process terminated gracefully", force_console=True)
            except subprocess.TimeoutExpired:
                log_message("Process not responding after 15 seconds, forcing kill...", force_console=True)
                context.process.kill()
                
                # Additional cleanup for stubborn processes
                try:
                    context.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log_message("Process forcefully terminated", force_console=True)
                
        except Exception as e:
            log_message(f"Error terminating process: {str(e)}", force_console=True)
    
    # Clean up progress bar
    if hasattr(context, 'pbar') and context.pbar:
        try:
            if hasattr(context.pbar, 'stop'):
                context.pbar.stop()
            elif hasattr(context.pbar, 'close'):
                context.pbar.close()
        except:
            pass
    
    log_message("Saving performance data before exit...", force_console=True)
    try:
        if ENABLE_PERFORMANCE_PROFILING and hasattr(context, 'profiler') and context.profiler is not None:
            save_performance_profile()
    except Exception as e:
        log_message(f"Error saving profile on exit: {str(e)}", force_console=True)
    
    log_message("Cleanup complete. Exiting...", force_console=True)
    sys.exit(0)

def monitor_progress():
    """Monitor processing progress and update progress bar with improved phase detection"""
    import time
    import re
    
    context.current_frame = 0
    
    try:
        if context.process is None:
            log_message("No process to monitor", force_console=True)
            return
            
        if RICH_AVAILABLE:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                TextColumn("[green]{task.fields[speed]}")
            )
            
            with progress:
                context.pbar = progress
                
                # Check if index file exists to determine initial state
                index_exists = check_existing_index_files(context.input_file)
                
                # Initialize tasks
                index_task = None
                process_task = None
                
                if index_exists:
                    # Index file exists - show completed status briefly then remove
                    index_task = progress.add_task("[green]✅ Index file exists", total=100, completed=100, speed="")
                    log_message("Index file exists - skipping indexing phase", force_console=True)
                    
                    # Remove index task after brief display
                    time.sleep(1)
                    try:
                        progress.remove_task(index_task)
                        index_task = None
                    except Exception as e:
                        log_message(f"Error removing index task: {e}", force_console=True)
                else:
                    # No index file - show indexing progress
                    index_task = progress.add_task("[yellow]Creating index file...", total=100, speed="")
                
                # Reset processing flags
                context.processing_task_created = False
                context.vapoursynth_started = False
                
                # Fallback timer
                start_time = time.time()
                fallback_triggered = False
                
                # Main monitoring loop
                try:
                    for line in iter(context.process.stderr.readline, ''):
                        if not line:
                            break
                            
                        line = line.strip()
                        if not line:
                            continue
                        
                        # Debug output
                        if DEBUG_MODE and line:
                            debug_log(f"STDERR: {line}")
                        
                        # Check if process terminated
                        if context.process.poll() is not None:
                            log_message("Process has terminated, stopping progress monitoring", force_console=True)
                            break
                        
                        # Check for VapourSynth processing start
                        should_start_processing = (
                            context.vapoursynth_started or 
                            "Input #0" in line or 
                            "Stream mapping:" in line or 
                            "Output #0" in line or
                            "Stream #0:0" in line or
                            "encoder" in line.lower() or
                            "yuv4mpegpipe" in line.lower() or
                            "[VHS_PROCESSING]" in line
                        )
                        
                        # Create processing task if needed
                        if not context.processing_task_created and should_start_processing:
                            try:
                                context.processing_task_created = True
                                
                                # Remove index task if it exists
                                if index_task is not None:
                                    try:
                                        progress.remove_task(index_task)
                                        index_task = None
                                    except:
                                        pass
                                
                                # Log detection source
                                if context.vapoursynth_started:
                                    log_message("Creating processing task: VapourSynth flag detected", force_console=True)
                                else:
                                    log_message(f"Creating processing task: FFmpeg pattern detected in: {line}", force_console=True)
                                
                                # Show phase transition
                                print("\n" + "="*60)
                                print("🎬 VAPOURSYNTH PROCESSING STARTED!")
                                print("="*60)
                                if TEST_MODE:
                                    print(f"📊 Processing {TEST_FRAME_COUNT} test frames")
                                    total_frames = TEST_FRAME_COUNT
                                    task_desc = f"[cyan]Processing {TEST_FRAME_COUNT} test frames..."
                                elif context.frame_count:
                                    print(f"📊 Processing {context.frame_count:,} total frames")
                                    total_frames = context.frame_count
                                    task_desc = f"[cyan]Processing {context.frame_count:,} frames..."
                                else:
                                    print("📊 Processing frames (total count unknown)")
                                    total_frames = None
                                    task_desc = "[cyan]Processing video frames..."
                                
                                print("⚡ Real-time progress will be shown below...")
                                print("="*60 + "\n")
                                
                                # Create processing task
                                process_task = progress.add_task(task_desc, total=total_frames, speed="0.0 fps")
                                log_message(f"Processing task created successfully with ID: {process_task}", force_console=True)

                                # Initialize timing markers so they're never None
                                context.last_frame_time  = time.time()
                                context.last_frame_count = 0
                                
                                # Initialize processing start time
                                context.processing_start_time = time.time()
                                
                            except Exception as e:
                                log_message(f"ERROR creating processing task: {e}", force_console=True)
                                import traceback
                                log_message(f"Traceback: {traceback.format_exc()}", force_console=True)
                        
                        # Monitor frame processing
                        elif context.processing_task_created and process_task is not None:
                            frame_detected = False
                            current_frame = 0
                            
                            # Frame detection patterns
                            frame_patterns = [
                                r'frame=\s*(\d+)',
                                r'Output (\d+) frames',
                                r'(\d+) frames in',
                            ]
                            
                            for pattern in frame_patterns:
                                try:
                                    frame_match = re.search(pattern, line, re.IGNORECASE)
                                    if frame_match:
                                        current_frame = int(frame_match.group(1))
                                        frame_detected = True
                                        break
                                except (ValueError, AttributeError):
                                    continue
                            
                            # Update progress
                            if frame_detected and current_frame > context.current_frame:
                                try:
                                    context.current_frame = current_frame
                                    current_time = time.time()
                                    
                                    # Calculate speed
                                    if context.last_frame_time is not None:
                                        # We have a valid previous timestamp, compute FPS
                                        time_diff  = current_time - context.last_frame_time
                                        frame_diff = current_frame - context.last_frame_count
                                        if time_diff > 0 and frame_diff > 0:
                                            fps        = frame_diff / time_diff
                                            speed_text = f"{fps:.1f} fps"
                                        else:
                                            fps        = 0
                                            speed_text = "calculating..."
                                    else:
                                        # First frame, no history yet
                                        fps        = 0
                                        speed_text = "starting..."
                                    
                                    # stamp the new values for next iteration
                                    context.last_frame_time  = current_time
                                    context.last_frame_count = current_frame
                                    
                                    # Update progress bar
                                    if TEST_MODE:
                                        progress.update(process_task, completed=min(current_frame, TEST_FRAME_COUNT), speed=speed_text)
                                    elif context.frame_count:
                                        progress.update(process_task, completed=current_frame, speed=speed_text)
                                        if fps > 0:
                                            remaining = context.frame_count - current_frame

                                            # Calculate average fps over the entire processing period
                                            elapsed = current_time - context.processing_start_time
                                            avg_fps = current_frame / elapsed if elapsed > 0 else fps

                                            eta_seconds = remaining / avg_fps
                                            eta_mins    = int(eta_seconds / 60)
                                            eta_hours   = eta_mins // 60
                                            eta_mins   %= 60

                                            eta_str = f"ETA: {eta_hours}h {eta_mins}m" if eta_hours else f"ETA: {eta_mins}m"
                                            progress.update(process_task, description=f"[cyan]Processing frames - {eta_str}")
                                    else:
                                        progress.update(process_task, completed=current_frame, total=current_frame + 1000, speed=speed_text)
                                    
                                except Exception as e:
                                    log_message(f"ERROR updating progress: {e}", force_console=True)
                        
                        # Fallback timer check
                        current_time = time.time()
                        if (not context.processing_task_created and 
                            not fallback_triggered and 
                            current_time - start_time > 10 and 
                            index_exists):
                            
                            fallback_triggered = True
                            log_message("Fallback: Creating processing task after 10 seconds", force_console=True)
                            
                            try:
                                context.processing_task_created = True
                                
                                print("\n" + "="*60)
                                print("🎬 PROCESSING STARTED (Fallback Detection)")
                                print("="*60)
                                
                                if TEST_MODE:
                                    total_frames = TEST_FRAME_COUNT
                                    task_desc = f"[cyan]Processing {TEST_FRAME_COUNT} test frames..."
                                elif context.frame_count:
                                    total_frames = context.frame_count
                                    task_desc = f"[cyan]Processing {context.frame_count:,} frames..."
                                else:
                                    total_frames = None
                                    task_desc = "[cyan]Processing video frames..."
                                
                                process_task = progress.add_task(task_desc, total=total_frames, speed="0.0 fps")
                                log_message(f"Fallback processing task created with ID: {process_task}", force_console=True)
                                
                                # Initialize processing start time in the fallback branch
                                context.processing_start_time = time.time()
                                
                            except Exception as e:
                                log_message(f"ERROR in fallback task creation: {e}", force_console=True)

                        # Call debug progress status periodically
                        if DEBUG_MODE and current_time - start_time > 5:  # Every 5 seconds after start
                            debug_progress_status()
                
                except Exception as e:
                    log_message(f"Error in main monitoring loop: {str(e)}", force_console=True)
                    import traceback
                    log_message(f"Traceback: {traceback.format_exc()}", force_console=True)
        
        else:
            # Fallback for systems without Rich - simplified version
            log_message("Rich not available, using simple progress monitoring", force_console=True)
            # ... existing non-Rich code ...
            
    except Exception as e:
        log_message(f"Error in progress monitoring: {str(e)}", force_console=True)
        import traceback
        log_message(f"Traceback: {traceback.format_exc()}", force_console=True)
    finally:
        if context.pbar:
            try:
                context.pbar.stop()
            except:
                pass
            context.pbar = None

def setup_venv_environment():
    """Configure environment to use venv packages with VapourSynth"""
    
    venv_python = sys.executable
    venv_dir = os.path.dirname(venv_python)
    venv_site_packages = os.path.join(venv_dir, "..", "Lib", "site-packages")
    venv_site_packages = os.path.abspath(venv_site_packages)
    
    env = os.environ.copy()
    
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = venv_site_packages + ";" + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = venv_site_packages
    
    env["PATH"] = venv_dir + ";" + env["PATH"]
    
    print(f"[VENV] Using Python: {venv_python}")
    print(f"[VENV] Added to PYTHONPATH: {venv_site_packages}")
    
    return env

def check_existing_index_files(input_file):
    """Check if index files already exist for the input file (cached)"""
    if context.index_status is not None:
        return context.index_status
        
    ffindex_file = input_file + ".ffindex"
    lwi_index_file = input_file + ".lwi"
    
    if os.path.exists(ffindex_file):
        file_size = os.path.getsize(ffindex_file) / (1024 * 1024)
        log_message(f"✅ Found existing FFmpeg index file: {os.path.basename(ffindex_file)} ({file_size:.1f} MB)")
        log_message("⚡ Indexing will be skipped - startup will be much faster!")
        context.index_status = "ffindex"
        return "ffindex"
    elif os.path.exists(lwi_index_file):
        file_size = os.path.getsize(lwi_index_file) / (1024 * 1024)
        log_message(f"✅ Found existing LWI index file: {os.path.basename(lwi_index_file)} ({file_size:.1f} MB)")
        log_message("⚡ Indexing will be skipped - startup will be much faster!")
        context.index_status = "lwi"
        return "lwi"
    else:
        log_message("⚠️  No existing index files found")
        log_message("⏳ New index file will be created (this may take 2-15 minutes)")
        context.index_status = None
        return None

def verify_output_quality():
    """Verify the output file integrity and quality"""
    if not os.path.exists(context.output_file):
        log_message("Output verification: File does not exist")
        return False
        
    file_size = os.path.getsize(context.output_file)
    if file_size < 1000000:  # Less than 1MB
        log_message(f"Output verification: File too small ({file_size} bytes)")
        return False
        
    try:
        probe_cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{context.output_file}"'
        result = subprocess.run(probe_cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0 and result.stdout.strip():
            duration_str = result.stdout.strip()
            if duration_str.lower() in ['n/a', 'na', '']:
                log_message("Output verification: Duration unavailable but file exists and has good size")
                return True
            try:
                duration = float(duration_str)
                log_message(f"Output verification: File is valid with duration {duration:.2f} seconds")
                return True
            except ValueError:
                log_message(f"Output verification: Could not parse duration '{duration_str}' but file appears valid")
                return True
        else:
            log_message(f"Output verification: ffprobe failed with return code {result.returncode}")
            return False
    except Exception as e:
        log_message(f"Output verification error: {str(e)}")
        return False

def detect_nvidia_gpu():
    """Detect if NVIDIA GPU is available for monitoring"""
    try:
        result = subprocess.run('nvidia-smi', capture_output=True, text=True, shell=True)
        return result.returncode == 0
    except:
        return False

def monitor_gpu_memory():
    """Check GPU memory usage once at startup"""
    if not context.has_nvidia_gpu:
        return
        
    try:
        cmd = 'nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            memory_used, memory_total = map(int, result.stdout.strip().split(','))
            usage_percent = (memory_used / memory_total) * 100
            
            log_message(f"GPU Memory at startup: {memory_used}MB / {memory_total}MB ({usage_percent:.1f}%)")
            
            if usage_percent > 80:
                log_message(f"WARNING: GPU memory usage is high at startup: {usage_percent:.1f}%")
    except Exception as e:
        log_message(f"Error checking GPU memory: {str(e)}")

class ProcessManager:
    """Manages process lifecycle and monitoring"""
    def __init__(self, cmd, env=None, timeout=30):
        self.cmd = cmd
        self.env = env
        self.timeout = timeout
        self.process = None
        self.stderr_output = []
        
    def start(self):
        """Start the process and monitoring"""
        try:
            log_message("Starting process with command: " + self.cmd)
            
            
            # Now start the full pipeline
            self.process = subprocess.Popen(
                self.cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                env=self.env,
                bufsize=0,  # Unbuffered for immediate output
                close_fds=False
            )
            
            # Start a thread to capture stderr
            import threading
            def capture_stderr():
                try:
                    for line in iter(self.process.stderr.readline, ''):
                        if line:
                            line = line.strip()
                            self.stderr_output.append(line)
                            # Log important messages immediately
                            if any(keyword in line.lower() for keyword in ['error', 'fail', 'unable', 'cannot']):
                                log_message(f"ERROR: {line}", force_console=True)
                            elif any(keyword in line for keyword in ['[FORMAT]', '[VHS_PROCESSING]', 'frame=']):
                                log_message(f"Process: {line}")
                except Exception as e:
                    log_message(f"Error in stderr capture: {e}")
            
            stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
            stderr_thread.start()
            
            # Give process a moment to start
            time.sleep(1.0)
            
            # Check if process is still running
            poll_result = self.process.poll()
            if poll_result is not None:
                log_message(f"❌ Process exited immediately with code: {poll_result}", force_console=True)
                
                # Wait a moment for stderr thread to capture output
                time.sleep(0.5)
                
                # Display captured stderr
                if self.stderr_output:
                    log_message("Captured error output:", force_console=True)
                    for line in self.stderr_output[-20:]:  # Last 20 lines
                        log_message(f"  > {line}", force_console=True)
                
                # Also try to get remaining output
                try:
                    remaining_out, remaining_err = self.process.communicate(timeout=1)
                    if remaining_out:
                        log_message(f"Stdout: {remaining_out[:500]}")
                    if remaining_err:
                        log_message(f"Additional stderr: {remaining_err[:500]}", force_console=True)
                except:
                    pass
                
                return None
            
            log_message("✅ Process started successfully and is running")
            return self.process
            
        except Exception as e:
            log_message(f"Error starting process: {str(e)}", force_console=True)
            import traceback
            log_message(f"Traceback: {traceback.format_exc()}")
            return None
    
    def stop(self):
        """Stop the process and cleanup"""
        if self.process and self.process.poll() is None:
            try:
                # Give process time to finish current frame
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log_message("Process didn't terminate gracefully, forcing kill", force_console=True)
                    self.process.kill()
            except Exception as e:
                log_message(f"Error during process termination: {str(e)}", force_console=True)
                
    def is_running(self):
        """Check if process is still running"""
        return self.process and self.process.poll() is None

def initialize_parallel():
    """Initialize components in parallel to reduce startup time"""
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        
        def init_vapoursynth():
            try:
                import vapoursynth as vs
                core = vs.core
                core.num_threads = multiprocessing.cpu_count()
                return True
            except Exception as e:
                log_message(f"Error initializing VapourSynth: {str(e)}")
                return False
        
        def init_filesystem():
            try:
                if not os.path.exists(INPUT_FILE_PATH):
                    log_message(f"ERROR: Input file does not exist: {INPUT_FILE_PATH}")
                    return False
                    
                if not os.path.exists(OUTPUT_DIRECTORY_PATH):
                    try:
                        os.makedirs(OUTPUT_DIRECTORY_PATH)
                    except Exception as e:
                        log_message(f"ERROR: Cannot create output directory: {str(e)}")
                        return False
                return True
            except Exception as e:
                log_message(f"Error in filesystem initialization: {str(e)}")
                return False
        
        def init_logging():
            try:
                with open(context.log_file, 'w') as f:
                    f.write("")
                return True
            except Exception as e:
                print(f"ERROR: Cannot initialize logging: {str(e)}")
                return False
        
        futures.append(executor.submit(init_vapoursynth))
        futures.append(executor.submit(init_filesystem))
        futures.append(executor.submit(init_logging))
        
        results = [f.result() for f in futures]
        return all(results)

def debug_progress_status():
    """Debug function to log current progress state"""
    if DEBUG_MODE:
        debug_log(f"Progress state: vapoursynth_started={context.vapoursynth_started}, "
                 f"processing_task_created={context.processing_task_created}, "
                 f"current_frame={context.current_frame}")
        if hasattr(context, 'pbar') and context.pbar:
            debug_log("Rich progress bar is active")
        else:
            debug_log("Rich progress bar is NOT active")

def print_processing_status():
    """Print detailed processing status to console"""
    print("\n" + "="*60)
    print("VAPOURSYNTH PROCESSING STATUS")
    print("="*60)
    
    if TEST_MODE:
        print(f"🧪 MODE: Test processing ({TEST_FRAME_COUNT} frames)")
    else:
        print(f"🎬 MODE: Full processing")
        if context.frame_count:
            print(f"📊 TOTAL FRAMES: {context.frame_count:,}")
    
    print(f"📁 INPUT: {os.path.basename(context.input_file)}")
    print(f"📁 OUTPUT: {os.path.basename(context.output_file)}")
    print(f"📂 RUN DIR: {os.path.basename(context.run_dir)}")
    
    # Show processing pipeline
    print("\n🔧 PROCESSING PIPELINE:")
    print("   1. ✅ FFmpegSource2 loading (FFV1 optimized)")
    print("   2. 🎯 QTGMC deinterlacing (Weave mode - 29.97fps)")
    print("   3. 🎨 Color space conversion (bt601 → bt709)")
    print("   4. ⬆️  Neural upscaling (NNEDI3)")
    print("   5. 📊 Y4M piping (automatic format handling)")
    print("   6. 🎞️  FFV1 encoding (10-bit 422 lossless)")
    
    print("="*60 + "\n")

def display_startup_status():
    """Display comprehensive startup status to user"""
    print("\n" + "="*80)
    print("🚀 VAPOURSYNTH VHS PROCESSING PIPELINE STARTING (Y4M MODE)")
    print("="*80)
    
    # Show file information
    file_size_mb = os.path.getsize(context.input_file) / (1024 * 1024)
    print(f"📁 INPUT FILE: {os.path.basename(context.input_file)}")
    print(f"💾 FILE SIZE: {file_size_mb:,.1f} MB")
    print(f"📂 RUN DIRECTORY: {os.path.basename(context.run_dir)}")
    
    if TEST_MODE:
        print(f"🧪 MODE: Test processing ({TEST_FRAME_COUNT} frames only)")
    else:
        print("🎬 MODE: Full file processing")
        if context.frame_count:
            print(f"📊 TOTAL FRAMES: {context.frame_count:,}")
            # Estimate processing time
            estimated_minutes = (context.frame_count / 100) * 7 / 60  # Based on ~15 fps processing
            if estimated_minutes > 60:
                hours = int(estimated_minutes / 60)
                mins = int(estimated_minutes % 60)
                print(f"⏱️  ESTIMATED TIME: ~{hours}h {mins}m")
            else:
                print(f"⏱️  ESTIMATED TIME: ~{estimated_minutes:.0f} minutes")
    
    print(f"📤 OUTPUT FILE: {os.path.basename(context.output_file)}")
    print("\n🔧 VHS PROCESSING PIPELINE (Y4M Workflow):")
    print("   1. 📂 Source loading (FFmpegSource2 - FFV1 optimized)")
    print("   2. 🎯 QTGMC deinterlacing (TFF mode for VHS sources)")
    print("   3. 🎨 Color space conversion (bt601 → bt709)")
    print("   4. ⬆️  Neural upscaling (NNEDI3 - doubles resolution)")
    print("   5. 📊 Y4M pipe output (automatic metadata handling)")
    print("   6. 🎞️  FFV1 encoding (10-bit 422 lossless)")
    print("   7. 📊 All outputs saved to timestamped subdirectory")
    print("\n✅ Y4M workflow eliminates pixel format mismatches!")
    print("="*80 + "\n")

# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # On Windows, set stdout to binary mode to prevent corrupting the Y4M stream
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    # Initialize timing
    context.start_time = time.time()
    processing_times['last_time'] = time.time()
    record_timing("initialization")

    # Set up venv environment
    context.venv_env = setup_venv_environment()

    # Set up paths
    context.input_file = INPUT_FILE_PATH
    output_base = OUTPUT_DIRECTORY_PATH

    # Check network drive accessibility
    network_drive = os.path.splitdrive(output_base)[0] + '\\'
    print(f"Checking network path: {output_base}")

    network_accessible = False
    try:
        if os.path.exists(output_base):
            network_accessible = True
            print(f"Output directory exists: {output_base}")
    except Exception as e:
        print(f"Error checking output directory: {str(e)}")

    if not network_accessible:
        print(f"ERROR: Network drive {network_drive} is not accessible.")
        print("Please ensure the network drive is properly mounted.")
        sys.exit(1)

    print(f"Network drive {network_drive} is accessible, proceeding with processing...")

    # Create timestamp and set up output paths with subdirectory
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    context.output_dir = output_base
    
    # Create timestamped subdirectory for this run
    run_subdir = f"vapoursynth_run_{timestamp}"
    context.run_dir = os.path.join(context.output_dir, run_subdir)
    
    # Create the run subdirectory
    try:
        os.makedirs(context.run_dir, exist_ok=True)
        log_message(f"Created run directory: {context.run_dir}")
    except Exception as e:
        print(f"ERROR: Cannot create run subdirectory: {context.run_dir}")
        print(f"Error details: {str(e)}")
        sys.exit(1)

    # Check if output directory exists and is writable
    try:
        if not os.path.exists(context.output_dir):
            print(f"ERROR: Output directory does not exist: {context.output_dir}")
            sys.exit(1)
        
        test_file = os.path.join(context.output_dir, '.write_test')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
    except (IOError, OSError) as e:
        print(f"ERROR: Cannot write to output directory: {context.output_dir}")
        print(f"Error details: {str(e)}")
        sys.exit(1)

    # Set up log file with proper error handling
    log_filename = f"vapoursynth_log_{timestamp}.txt"
    context.log_file = os.path.join(context.run_dir, log_filename)

    # Ensure the directory exists and is writable
    try:
        os.makedirs(context.run_dir, exist_ok=True)
        # Test write access
        with open(context.log_file, 'w') as f:
            f.write(f"VapourSynth VHS Processing Log - {timestamp}\n")
            f.write("="*50 + "\n\n")
        log_message("Log file initialized successfully")
    except (IOError, OSError) as e:
        print(f"WARNING: Cannot create log file at {context.log_file}")
        print(f"Error details: {str(e)}")
        # Use a fallback location
        fallback_log = os.path.join(os.getcwd(), log_filename)
        try:
            with open(fallback_log, 'w') as f:
                f.write(f"VapourSynth VHS Processing Log - {timestamp}\n")
            context.log_file = fallback_log
            print(f"Using fallback log location: {fallback_log}")
        except:
            print("WARNING: Logging to file disabled - console output only")
            context.log_file = None

    # Initialize profiler
    if ENABLE_PERFORMANCE_PROFILING:
        try:
            import cProfile
            context.profiler = cProfile.Profile()
            context.profiler.enable()
            print("Performance profiling enabled - collecting data...")
        except ImportError:
            print("Profiling modules not available - performance profiling disabled")

    # Initialize NVIDIA GPU detection
    context.has_nvidia_gpu = detect_nvidia_gpu()
    if context.has_nvidia_gpu:
        log_message("NVIDIA GPU detected - GPU memory monitoring available")
    else:
        log_message("No NVIDIA GPU detected")

    # --- FIX: Force the use of the local upscale.vpy script ---
    script_path = "upscale.vpy"
    
    # Verify script exists
    if not os.path.exists(script_path):
        log_message(f"ERROR: VapourSynth script not found at: {script_path}")
        log_message(f"Please ensure the script exists in the same directory as the pipeline script.")
        sys.exit(1)
    else:
        log_message(f"✅ VapourSynth script found: {os.path.abspath(script_path)}")
    
    input_basename = os.path.splitext(os.path.basename(context.input_file))[0]
    
    # --- FIX: Dynamic output file extension based on format ---
    output_extension = ".mov" if USE_PRORES_OUTPUT else ".mkv"
    context.output_file = os.path.join(context.run_dir, f"{input_basename}_VHS_Y4M_{timestamp}{output_extension}")

    log_message(f"Using input file: {context.input_file}")
    log_message(f"Using output directory: {context.output_dir}")
    log_message(f"Using run subdirectory: {context.run_dir}")

    # Check if input file exists and analyze format
    if not os.path.exists(context.input_file):
        log_message(f"ERROR: Input file does not exist: {context.input_file}")
        sys.exit(1)
    else:
        file_size = os.path.getsize(context.input_file) / (1024 * 1024)
        log_message(f"Input file exists: {context.input_file} (Size: {file_size:.2f} MB)")
        
        # Analyze FFV1 source format for compatibility
        try:
            probe_cmd = f'ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pix_fmt,width,height,field_order -of default=noprint_wrappers=1 "{context.input_file}"'
            result = subprocess.run(probe_cmd, capture_output=True, text=True, shell=True, timeout=10)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                codec_name = field_order = pix_fmt = width = height = "unknown"
                for line in lines:
                    if line.startswith('codec_name='):
                        codec_name = line.split('=')[1]
                    elif line.startswith('pix_fmt='):
                        pix_fmt = line.split('=')[1]
                    elif line.startswith('width='):
                        width = line.split('=')[1]
                    elif line.startswith('height='):
                        height = line.split('=')[1]
                    elif line.startswith('field_order='):
                        field_order = line.split('=')[1]
                
                log_message(f"Source format analysis:")
                log_message(f"  Codec: {codec_name}")
                log_message(f"  Pixel format: {pix_fmt}")
                log_message(f"  Resolution: {width}x{height}")
                log_message(f"  Field order: {field_order}")
                
                # FFV1 10-bit 422 compatibility check
                if codec_name == "ffv1":
                    log_message("✅ FFV1 codec detected - excellent for lossless processing")
                    if "422" in pix_fmt and "10" in pix_fmt:
                        log_message("✅ 10-bit 422 format detected - optimal for quality retention")
                    else:
                        log_message(f"⚠️  Pixel format {pix_fmt} detected - pipeline optimized for 10-bit 422")
                else:
                    log_message(f"ℹ️  Non-FFV1 codec detected: {codec_name}")
                
                # Field order detection for VHS
                if field_order == "tt" or field_order == "tb":
                    log_message("✅ Top Field First (TFF) detected - standard for VHS")
                elif field_order == "bb" or field_order == "bt":
                    log_message("⚠️  Bottom Field First (BFF) detected - unusual for VHS")
                else:
                    log_message(f"ℹ️  Field order: {field_order} - assuming TFF for VHS")
                    
        except Exception as e:
            log_message(f"Could not analyze source format: {e}")
            log_message("Proceeding with default processing pipeline")

    # Check for existing index files before processing with enhanced user feedback
    print("\n" + "="*60)
    print("CHECKING FOR EXISTING INDEX FILES")
    print("="*60)

    index_status = check_existing_index_files(context.input_file)

    if index_status:
        print("✅ INDEX FILE FOUND - FAST STARTUP ENABLED!")
        print(f"   Using existing {index_status} index file")
        print("   Skipping indexing phase - processing will start immediately")
        print("⚡ This will save 2-15 minutes of indexing time!")
        log_message(f"Index file found: {index_status} - fast startup enabled", force_console=True)
    else:
        print("⚠️  NO INDEX FILE FOUND")
        print("🔍 Creating new index file (this may take 2-15 minutes)")
        print("💡 TIP: Once created, the index file will be reused for faster future runs")
        log_message("No index file found - will create new index file", force_console=True)

    print("="*60 + "\n")

    # Initialize parallel processing
    if not initialize_parallel():
        log_message("Initialization failed. Please check the logs for details.")
        sys.exit(1)

    record_timing("initialization_complete")

    # Start logging
    log_message("VHS Processing started with Y4M workflow")
    if TEST_MODE:
        log_message(f"🧪 TEST MODE ENABLED - Processing only first {TEST_FRAME_COUNT} frames")
    else:
        log_message("🎬 FULL PROCESSING MODE - Processing entire video file")
    
    log_message("ℹ️  Y4M pipeline features:")
    log_message("   - Y4M piping eliminates pixel format mismatches")
    log_message("   - Automatic dimension and frame rate handling")
    log_message("   - No manual width/height/format configuration needed")
    log_message("   - FFmpegSource2 for fast indexing")
    log_message("   - TFF field order handling for VHS sources")
    log_message("   - QTGMC Weave mode to maintain 29.97fps")
    log_message("   - bt601→bt709 color space conversion")
    log_message("   - YUV422P10 preserved throughout pipeline")
    
    record_timing("start_processing")

    # Determine total frame count via metadata
    try:
        # Try reading the container's nb_frames header (no full decode)
        probe_cmd = (
            f'ffprobe -v error -select_streams v:0 '
            f'-show_entries stream=nb_frames '
            f'-of default=noprint_wrappers=1:nokey=1 "{context.input_file}"'
        )
        result = subprocess.run(probe_cmd,
                                capture_output=True, text=True,
                                shell=True, timeout=10)
        stdout = result.stdout.strip()
        if result.returncode == 0 and stdout.isdigit():
            context.frame_count = int(stdout)
            log_message(f"Frame count detected via metadata: {context.frame_count}")
        else:
            # Fallback: compute from duration × framerate
            duration_cmd = (
                f'ffprobe -v error '
                f'-show_entries format=duration '
                f'-of default=noprint_wrappers=1:nokey=1 "{context.input_file}"'
            )
            dur_res = subprocess.run(duration_cmd,
                                     capture_output=True, text=True,
                                     shell=True, timeout=10)
            duration = float(dur_res.stdout.strip())

            fr_cmd = (
                f'ffprobe -v error -select_streams v:0 '
                f'-show_entries stream=r_frame_rate '
                f'-of default=noprint_wrappers=1:nokey=1 "{context.input_file}"'
            )
            fr_res = subprocess.run(fr_cmd,
                                    capture_output=True, text=True,
                                    shell=True, timeout=10)
            fr_str = fr_res.stdout.strip()
            if '/' in fr_str:
                num, den = map(int, fr_str.split('/'))
                fr = num/den
            else:
                fr = float(fr_str)

            context.frame_count = int(duration * fr)
            log_message(
                f"Frame count estimated from duration ({duration:.2f}s) "
                f"* framerate ({fr:.2f}fps): {context.frame_count}"
            )
    except Exception as e:
        # Ultimate fallback
        context.frame_count = None
        log_message(f"Could not determine frame count: {e}")

    # ============================================================================
    # COMMAND WILL BE BUILT AFTER TESTING WHICH METHOD WORKS
    # ============================================================================
    
    # Set environment variable for the VapourSynth script (as backup/primary method)
    context.venv_env["VS_INPUT_FILE"] = context.input_file
    
    # Debug: Log the environment variable being set
    log_message(f"Setting VS_INPUT_FILE environment variable to: {context.input_file}")
    log_message(f"Environment check: VS_INPUT_FILE = {context.venv_env.get('VS_INPUT_FILE', 'NOT SET')}")
    
    # Note: The actual command will be built in the test section based on which method works
    # (either --arg or environment variable)
    
    # Get source frame rate for reference
    try:
        probe_cmd = f'ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of default=noprint_wrappers=1:nokey=1 "{context.input_file}"'
        result = subprocess.run(probe_cmd, capture_output=True, text=True, shell=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            framerate_str = result.stdout.strip()
            if '/' in framerate_str:
                num, den = map(int, framerate_str.split('/'))
                framerate = num / den
            else:
                framerate = float(framerate_str)
            log_message(f"Detected source frame rate: {framerate:.6f} fps ({framerate_str})")
        else:
            framerate = 29.97  # Default fallback
            log_message(f"Could not detect frame rate, using default: {framerate} fps")
    except Exception as e:
        framerate = 29.97
        log_message(f"Frame rate detection error: {e}, using default: {framerate} fps")

    # Display comprehensive startup status to user
    display_startup_status()

    # --- NEW: Reliable script testing block ---
    if TEST_MODE:
        log_message("Testing VapourSynth script before processing...")
        
        # Ensure vspipe is available
        try:
            result = subprocess.run("vspipe --version", shell=True, capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                log_message("❌ vspipe not found or not working!", force_console=True)
                sys.exit(1)
            log_message(f"✅ vspipe is available: {result.stdout.strip()}")
        except Exception as e:
            log_message(f"❌ Could not run vspipe: {e}", force_console=True)
            sys.exit(1)

        # Test the actual script that will be used
        test_cmd = f'vspipe --info "{script_path}" -'
        log_message(f"Running test command: {test_cmd}")
        
        try:
            test_result = subprocess.run(
                test_cmd,
                shell=True,
                capture_output=True,
                text=True,
                env=context.venv_env,
                timeout=30
            )
            if test_result.returncode == 0:
                log_message("✅ VapourSynth script loaded successfully for testing.")
                # Log script output details
                output = test_result.stdout + test_result.stderr
                for line in output.split('\n'):
                    if any(keyword in line for keyword in ['Width:', 'Height:', 'Frames:', 'FPS:', 'Format:']):
                        log_message(f"   {line.strip()}")
            else:
                log_message("❌ VapourSynth script failed to load during test!", force_console=True)
                log_message(f"   Exit code: {test_result.returncode}", force_console=True)
                if test_result.stderr:
                    log_message("   Error output:", force_console=True)
                    for line in test_result.stderr.split('\n'):
                        if line.strip():
                            log_message(f"     > {line}", force_console=True)
                sys.exit(1)
        except subprocess.TimeoutExpired:
            log_message("⚠️  Script test timed out. This can happen if it's creating an index file.", force_console=True)
            log_message("   Proceeding with main processing...")
        except Exception as e:
            log_message(f"❌ An unexpected error occurred during script testing: {e}", force_console=True)
            sys.exit(1)
    
    # Build command using environment variable method
    log_message("Building processing command using environment variable method...")
    
    # --- FIX: Conditional FFmpeg command based on output format ---
    if USE_PRORES_OUTPUT:
        # ProRes 422 HQ command
        ffmpeg_command = (
            f'ffmpeg -f yuv4mpegpipe -i - '
            f'-c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le '
            f'-aspect 4:3 '
            f'-color_range tv -colorspace bt709 '
            f'-video_track_timescale 30000 -y "{context.output_file}"'
        )
    else:
        # FFV1 (default) command
        ffmpeg_command = (
            f'ffmpeg -f yuv4mpegpipe -i - '
            f'-c:v ffv1 -level 3 -pix_fmt yuv422p10le '
            f'-aspect 4:3 '
            f'-color_range tv -colorspace bt709 '
            f'-video_track_timescale 30000 -y "{context.output_file}"'
        )

    if TEST_MODE:
        context.cmd = (
            f'vspipe -c y4m "{script_path}" - | '
            f'{ffmpeg_command.replace("-y", f"-frames:v {TEST_FRAME_COUNT} -y")}'
        )
    else:
        context.cmd = f'vspipe -c y4m "{script_path}" - | {ffmpeg_command}'

    
    log_message(f"Final command: {context.cmd}")
    
    # Initialize process manager and start processing
    context.process_manager = ProcessManager(context.cmd, env=context.venv_env, timeout=30)
    context.process = context.process_manager.start()

    if not context.process:
        log_message("Failed to start processing. Check errors above.", force_console=True)
        
        # Try to provide more diagnostic info
        log_message("\n🔍 Diagnostic Information:", force_console=True)
        log_message(f"  Script path: {script_path}", force_console=True)
        log_message(f"  Input file: {context.input_file}", force_console=True)
        log_message(f"  Output file: {context.output_file}", force_console=True)
        log_message(f"  VS_INPUT_FILE env var: {context.venv_env.get('VS_INPUT_FILE', 'NOT SET')}", force_console=True)
        
        # Check if we can at least run vspipe alone
        simple_vspipe_test = f'vspipe --info "{script_path}" -'
        log_message("\nTrying basic vspipe test...", force_console=True)
        try:
            test_res = subprocess.run(
                simple_vspipe_test,
                shell=True,
                capture_output=True,
                text=True,
                env=context.venv_env,
                timeout=10
            )
            if test_res.returncode == 0:
                log_message("✅ VapourSynth script can be loaded", force_console=True)
                log_message("❌ Issue is likely with piping or ffmpeg", force_console=True)
            else:
                log_message("❌ VapourSynth script cannot be loaded", force_console=True)
                if test_res.stderr:
                    log_message(f"Error: {test_res.stderr[:500]}", force_console=True)
        except Exception as e:
            log_message(f"Test failed: {e}", force_console=True)
        
        sys.exit(1)

    record_timing("process_start")
    record_timing("main_processing")

    # Start monitoring threads
    log_message("Starting monitoring threads...")

    progress_thread = threading.Thread(target=monitor_progress, name="ProgressMonitor")
    progress_thread.daemon = True
    progress_thread.start()

    # Check GPU status once at startup
    if context.has_nvidia_gpu and ENABLE_GPU_MONITORING:
        monitor_gpu_memory()

    record_timing("progress_monitoring_started")

    # Wait for process to complete
    actual_frames_processed = 0
    try:
        while context.process.poll() is None:
            try:
                context.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # Check if we're actually processing frames
                if context.current_frame > 0:
                    actual_frames_processed = context.current_frame
                continue
            except KeyboardInterrupt:
                log_message("Keyboard interrupt detected, terminating process...", force_console=True)
                context.process_manager.stop()
                signal_handler(signal.SIGINT, None)
                break
    except Exception as e:
        log_message(f"Error during process execution: {str(e)}", force_console=True)
    finally:
        # Get final frame count
        if context.current_frame > 0:
            actual_frames_processed = context.current_frame
        context.process_manager.stop()

    # Cleanup progress bar
    if context.pbar:
        if hasattr(context.pbar, 'close'):
            context.pbar.close()
    record_timing("process_complete")

    total_time = time.time() - context.start_time

    # Check results
    output_exists = os.path.exists(context.output_file)
    output_size = os.path.getsize(context.output_file) if output_exists else 0
    
    if TEST_MODE:
        if actual_frames_processed > 0:
            log_message(f"Test mode complete: Processed {actual_frames_processed} frames")
        else:
            log_message(f"Test mode failed: No frames were processed", force_console=True)
            
        if output_exists and output_size > 1000000:  # More than 1MB
            log_message(f"✅ Output file created: {output_size / (1024*1024):.2f} MB")
        else:
            log_message(f"❌ No valid output file created", force_console=True)
            
    elif context.process.returncode == 0 and output_exists and output_size > 1000000:
        log_message(f"Success! Output saved to: {context.output_file}")
        log_message(f"Output file size: {output_size / (1024 * 1024):.2f} MB")
        log_message(f"Frames processed: {actual_frames_processed}")
    else:
        log_message("Error during processing!", force_console=True)
        if not output_exists:
            log_message("Output file was not created", force_console=True)
        elif output_size < 1000000:
            log_message(f"Output file too small: {output_size} bytes", force_console=True)
        if context.process.returncode != 0:
            log_message(f"Process exited with code: {context.process.returncode}", force_console=True)
        log_message(f"Frames actually processed: {actual_frames_processed}", force_console=True)

    log_message(f"Total processing time: {total_time:.2f} seconds ({total_time/60:.2f} minutes)")
    if total_time > 0 and actual_frames_processed > 0:
        log_message(f"Average processing speed: {actual_frames_processed/total_time:.2f} frames per second")
    log_message("VHS processing completed.")
    record_timing("completion")

    # Verify output quality
    if os.path.exists(context.output_file):
        if verify_output_quality():
            log_message("Output file verification successful.")
        else:
            log_message("WARNING: Output file verification failed, file may be corrupted.")

    # Save performance profile
    if ENABLE_PERFORMANCE_PROFILING and context.profiler:
        log_message("Generating performance profile...")
        save_performance_profile()
        log_message("Performance profile generation complete")

    # --- FIX: Replace final status block with a fun success message ---
    success_message = f"\n✅ [bold green]Processing complete! Your final video can be found here -->[/bold green] {context.output_file}"
    if RICH_AVAILABLE:
        console = Console()
        console.print(success_message)
    else:
        print(f"\n✅ Processing complete! Your final video can be found here --> {context.output_file}")

if __name__ == "__main__":
    main()