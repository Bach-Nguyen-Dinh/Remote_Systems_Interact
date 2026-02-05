#!/usr/bin/env python3
"""
Simple Demo Launcher GUI
Select and run demo scenarios with one click.
"""

import tkinter as tk
from tkinter import messagebox
import subprocess
import os
import signal

CURR_DIR = os.path.dirname(os.path.abspath(__file__))

DEMOS = {
    "HPC Standalone": os.path.join(CURR_DIR, "launch_hpc_standalone_demo.py"),
    "Topaz Standalone": os.path.join(CURR_DIR, "launch_topaz2_standalone_demo.py"),
    "Dual Target (HPC + Topaz)": os.path.join(CURR_DIR, "launch_dual_target_demo.py"),
}


class DemoLauncher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Demo Launcher")
        self.root.geometry("400x350")
        self.root.resizable(False, False)

        self.current_process = None
        self.current_demo = None

        self.build_ui()

    def build_ui(self):
        # Title
        title = tk.Label(self.root, text="Select Demo", font=("Arial", 16, "bold"))
        title.pack(pady=15)

        # Demo buttons
        self.buttons = {}
        for name, script in DEMOS.items():
            btn = tk.Button(
                self.root,
                text=name,
                font=("Arial", 12),
                width=30,
                height=2,
                command=lambda n=name, s=script: self.launch_demo(n, s)
            )
            btn.pack(pady=5)
            self.buttons[name] = btn

        # Status
        self.status_frame = tk.Frame(self.root)
        self.status_frame.pack(pady=20)

        self.status_indicator = tk.Canvas(self.status_frame, width=20, height=20)
        self.status_indicator.pack(side=tk.LEFT, padx=5)
        self.indicator_circle = self.status_indicator.create_oval(2, 2, 18, 18, fill="gray")

        self.status_label = tk.Label(self.status_frame, text="No demo running", font=("Arial", 10))
        self.status_label.pack(side=tk.LEFT)

        # Stop button
        self.stop_btn = tk.Button(
            self.root,
            text="Stop Current Demo",
            font=("Arial", 11),
            width=20,
            bg="#d9534f",
            fg="white",
            state=tk.DISABLED,
            command=self.stop_demo
        )
        self.stop_btn.pack(pady=10)

    def launch_demo(self, name, script):
        # Stop current demo if running
        if self.current_process:
            self.stop_demo()

        # Launch new demo
        try:
            self.current_process = subprocess.Popen(
                ["python3", script],
                preexec_fn=os.setsid  # Create new process group for clean termination
            )
            self.current_demo = name
            self.update_status(name, running=True)

            # Highlight active button
            for btn_name, btn in self.buttons.items():
                if btn_name == name:
                    btn.config(bg="#5cb85c", fg="white")
                else:
                    btn.config(bg="#d9d9d9", fg="black")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch demo:\n{e}")

    def stop_demo(self):
        if self.current_process:
            try:
                # Kill entire process group
                os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
                self.current_process.wait(timeout=5)
            except:
                try:
                    os.killpg(os.getpgid(self.current_process.pid), signal.SIGKILL)
                except:
                    pass

            self.current_process = None
            self.current_demo = None
            self.update_status(None, running=False)

            # Reset button colors
            for btn in self.buttons.values():
                btn.config(bg="#d9d9d9", fg="black")

    def update_status(self, demo_name, running):
        if running:
            self.status_indicator.itemconfig(self.indicator_circle, fill="green")
            self.status_label.config(text=f"Running: {demo_name}")
            self.stop_btn.config(state=tk.NORMAL)
        else:
            self.status_indicator.itemconfig(self.indicator_circle, fill="gray")
            self.status_label.config(text="No demo running")
            self.stop_btn.config(state=tk.DISABLED)

    def on_close(self):
        if self.current_process:
            if messagebox.askyesno("Confirm Exit", "A demo is running. Stop it and exit?"):
                self.stop_demo()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()


if __name__ == "__main__":
    app = DemoLauncher()
    app.run()
