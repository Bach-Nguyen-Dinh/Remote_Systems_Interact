from flask import Flask, request, jsonify # type: ignore
from flask_cors import CORS  # type: ignore # Import CORS
import socket

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

TARGET_IP = "10.42.0.91"  # target system IP
TARGET_PORT = 54321      # Port on which the target system is listening

@app.route('/send_message', methods=['POST'])
def send_message():
    data = request.get_json()
    message = data.get("message", "Default message from host")
    print(f"Message from host: {message}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
            client_socket.connect((TARGET_IP, TARGET_PORT))
            client_socket.sendall(message.encode())
        return jsonify({"status": "success", "message": message})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
