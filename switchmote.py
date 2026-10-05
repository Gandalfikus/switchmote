#!/usr/bin/env python3
"""Switchmote - Bluetooth Controller Manager & Xbox-Mapper (With Calibration & Settings)"""
import json
import math
import os
import queue
import re
import selectors
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font, messagebox, simpledialog, ttk

try:
    import evdev
    from evdev import ecodes as e
except ImportError as err:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Kritischer Fehler",
        f"Die gepackte 'evdev'-Bibliothek konnte nicht geladen werden.\nDetails: {err}"
    )
    sys.exit(1)

MAX_SLOTS = 4
CONFIG_DIR = Path.home() / ".config" / "switchmote"
CONFIG_FILE = CONFIG_DIR / "slots.json"
LABELS_FILE = CONFIG_DIR / "labels.json"
LOG_FILE = CONFIG_DIR / "connection_log.csv"

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02]")
DEVICE_RE = re.compile(r"Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)")
MAC_REGEX = re.compile(r"([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})")
HINTS = ("controller", "gamepad", "joy-con", "nintendo", "rvl-", "wiimote", "xbox", "8bitdo")
GREEN, ORANGE, RED, GRAY = "#2e7d32", "#ef6c00", "#c62828", "#757575"

ICON_B64 = b'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAC0SURBVDhPrZDRDYAgDEWf0Y3cxZ3cwQ10I8YQMBRKizH+mDRpeU9rAQyI14QhB4yQk1P2EGEJ6AsYohbYc42m3sFMyiV+0H0h1yLzP5C7F+TqBSd7QeIbcF9IGcDHfQA/51s10L0xR/T/QNfGHIH2A0lKk3h5V6bYc0C2zTmixAHLNueIPgf0bM0R4/kDUX2BvIEqIqB/QA8P6HwY4uIFuXvBzV+QmD/QfSGPgHlD1sC8IeEbwB/zBlh+gLEAAAAASUVORD5CYII='

MAP_KEYS = {
    e.KEY_UP: e.BTN_DPAD_UP, e.KEY_DOWN: e.BTN_DPAD_DOWN, e.KEY_LEFT: e.BTN_DPAD_LEFT, e.KEY_RIGHT: e.BTN_DPAD_RIGHT,
    e.KEY_PREVIOUS: e.BTN_SELECT, e.KEY_NEXT: e.BTN_START, e.BTN_SELECT: e.BTN_SELECT, e.BTN_START: e.BTN_START,
    e.BTN_MODE: e.BTN_MODE, e.KEY_HOMEPAGE: e.BTN_MODE, 257: e.BTN_X, 258: e.BTN_Y,
    e.BTN_A: e.BTN_SOUTH, e.BTN_B: e.BTN_EAST, e.BTN_X: e.BTN_WEST, e.BTN_Y: e.BTN_NORTH,
    e.BTN_TL: e.BTN_TL, e.BTN_TR: e.BTN_TR, e.BTN_C: e.BTN_TL, e.BTN_Z: e.BTN_TR,
}

UINPUT_CAPS = {
    e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL, e.BTN_TR, e.BTN_TL2, e.BTN_TR2,
               e.BTN_SELECT, e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR,
               e.BTN_DPAD_UP, e.BTN_DPAD_DOWN, e.BTN_DPAD_LEFT, e.BTN_DPAD_RIGHT],
    e.EV_ABS: [(e.ABS_X, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)), (e.ABS_Y, evdev.AbsInfo(0, -32768, 32767, 16, 128, 0)),
               (e.ABS_Z, evdev.AbsInfo(0, 0, 255, 0, 0, 0)), (e.ABS_RZ, evdev.AbsInfo(0, 0, 255, 0, 0, 0)),
               (e.ABS_HAT0X, evdev.AbsInfo(0, -1, 1, 0, 0, 0)), (e.ABS_HAT0Y, evdev.AbsInfo(0, -1, 1, 0, 0, 0))]
}

SHAKE_COOLDOWN = 0.35    
TILT_SENSITIVITY = 1000  

def clean_mac(s):
    if not s: return None
    m = MAC_REGEX.search(str(s))
    return m.group(1).replace("-", ":").upper() if m else None

def resolve_ecode(mapping, code, default_prefix, custom_labels=None):
    val = mapping.get(code)
    if not val: raw = f"{default_prefix}_{code}"
    elif isinstance(val, (list, tuple)): raw = str(val[0]) if val else f"{default_prefix}_{code}"
    else: raw = str(val)
    if custom_labels and raw in custom_labels and custom_labels[raw].strip():
        return f"{custom_labels[raw]} ({raw})"
    return raw

def get_battery_info(mac, bt_out=""):
    for line in bt_out.splitlines():
        if "Battery Percentage" in line or "Battery" in line:
            m = re.search(r"\(?(\d+)%\)?", line) or re.search(r"(\d+)", line)
            if m: return f"{m.group(1)}%"
    if mac:
        clean = mac.replace(":", "").lower()
        try:
            for p in Path("/sys/class/power_supply").glob("*"):
                if clean in p.name.lower().replace(":", ""):
                    cap = p / "capacity"
                    if cap.exists(): return f"{cap.read_text().strip()}%"
        except Exception: pass
    return None

def btctl(*args, timeout=15):
    try: p = subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired: return "Zeitüberschreitung"
    except OSError as err: return f"bluetoothctl Fehler: {err}"
    return ANSI.sub("", (p.stdout or "") + (p.stderr or "")).strip()

def parse_info(out, mac=None):
    d = {"known": "Device" in out and "not available" not in out, "name": None,
         "paired": False, "trusted": False, "connected": False, "battery": None, "rssi": None}
    for line in out.splitlines():
        key, _, val = line.strip().partition(":")
        val = val.strip()
        if key == "Name": d["name"] = val
        elif key == "Alias" and not d["name"]: d["name"] = val
        elif key in ("Paired", "Trusted", "Connected"): d[key.lower()] = val == "yes"
        elif key == "RSSI": d["rssi"] = val.replace("dBm", "").strip()
    
    d["battery"] = get_battery_info(mac, out)
    return d

def parse_devices(out):
    return {m.group(1).upper(): m.group(2).strip() or m.group(1).upper() 
            for line in out.splitlines() if (m := DEVICE_RE.search(line))}

def get_grouped_devices():
    mac_groups, parent_groups = {}, {}
    for path in evdev.list_devices():
        try: d = evdev.InputDevice(path)
        except (OSError, PermissionError): continue

        mac = clean_mac(d.uniq)
        if not mac:
            try:
                sysfs_path = os.path.realpath(f"/sys/class/input/{os.path.basename(path)}/device")
                mac = clean_mac(sysfs_path)
            except OSError: pass

        if mac: mac_groups.setdefault(mac, []).append(d.path)
        else:
            try:
                sysfs = os.path.realpath(f"/sys/class/input/{os.path.basename(path)}/device")
                parent = sysfs.split("/input/")[0] if "/input/" in sysfs else sysfs
                parent_groups.setdefault(parent, []).append((d.path, d.name, d.phys))
            except OSError: pass

    for parent, dev_tuples in parent_groups.items():
        group_mac, paths = None, []
        for p, name, phys in dev_tuples:
            paths.append(p)
            m = clean_mac(phys)
            if m and not group_mac: group_mac = m
        if group_mac: mac_groups.setdefault(group_mac, []).extend(paths)

    return mac_groups

class SwitchmoteMapper(threading.Thread):
    def __init__(self, app, slot, mac, device_paths):
        super().__init__(daemon=True)
        self.app, self.slot, self.mac, self.device_paths = app, slot, mac, device_paths
        self.stop_ev = threading.Event()
        
        # Sensorzustände & Kalibrierung
        self.raw_rx, self.raw_ry, self.raw_rz = 0, 0, 0
        self.prev_mag, self.last_shake = 0.0, 0.0
        
        self.calibrating = False
        self.calib_samples = []
        
        saved_offsets = self.app.offsets.get(self.mac, {})
        self.offset_rx = saved_offsets.get("rx", -123)
        self.offset_ry = saved_offsets.get("ry", -35)
        self.offset_rz = saved_offsets.get("rz", -27)

    def start_calibration(self):
        self.calib_samples.clear()
        self.calibrating = True

    def finish_calibration(self):
        self.calibrating = False
        if self.calib_samples:
            self.offset_rx = sum(s[0] for s in self.calib_samples) / len(self.calib_samples)
            self.offset_ry = sum(s[1] for s in self.calib_samples) / len(self.calib_samples)
            self.offset_rz = sum(s[2] for s in self.calib_samples) / len(self.calib_samples)
        return self.offset_rx, self.offset_ry, self.offset_rz

    def run(self):
        devices = []
        for p in self.device_paths:
            try:
                d = evdev.InputDevice(p)
                d.grab()
                devices.append(d)
            except Exception as err: self.app.log(f"Fehler beim Grab von {p}: {err}")
        if not devices: return

        try:
            ui = evdev.UInput(events=UINPUT_CAPS, name=f"Microsoft X-Box 360 pad (Switchmote {self.slot})", vendor=0x045e, product=0x028e, version=0x0110)
        except Exception as err:
            self.app.log(f"Virtuelles Gerät {self.slot} blockiert: {err}")
            for d in devices:
                try: d.ungrab()
                except: pass
                d.close()
            return

        sel = selectors.DefaultSelector()
        for d in devices: sel.register(d, selectors.EVENT_READ)

        try:
            while not self.stop_ev.is_set():
                for key, _ in sel.select(timeout=0.1):
                    d = key.fileobj
                    try:
                        for ev in d.read():
                            if self.app.dbg and getattr(self.app.dbg, 'active_mac', None) == self.mac:
                                try: self.app.debug_q.put_nowait((d.name, ev.type, ev.code, ev.value))
                                except queue.Full: pass

                            if ev.type == e.EV_KEY:
                                code = MAP_KEYS.get(ev.code, ev.code)
                                if code in UINPUT_CAPS[e.EV_KEY]: ui.write(e.EV_KEY, code, ev.value)

                            elif ev.type == e.EV_ABS:
                                if ev.code in (e.ABS_RX, e.ABS_RY, e.ABS_RZ):
                                    if ev.code == e.ABS_RX: self.raw_rx = ev.value
                                    elif ev.code == e.ABS_RY: self.raw_ry = ev.value
                                    elif ev.code == e.ABS_RZ: self.raw_rz = ev.value

                                    if self.calibrating:
                                        self.calib_samples.append((self.raw_rx, self.raw_ry, self.raw_rz))
                                        continue 

                                    cx = self.raw_rx - self.offset_rx
                                    cy = self.raw_ry - self.offset_ry
                                    cz = self.raw_rz - self.offset_rz

                                    # Geflippte X-Achse (-cy), damit Links/Rechts korrekt steuern
                                    stick_x = max(-32768, min(32767, int(-cy * TILT_SENSITIVITY)))
                                    stick_y = max(-32768, min(32767, int(cx * TILT_SENSITIVITY)))
                                    
                                    ui.write(e.EV_ABS, e.ABS_X, stick_x)
                                    ui.write(e.EV_ABS, e.ABS_Y, stick_y)

                                    mag = math.sqrt(cx**2 + cy**2 + cz**2)
                                    delta_mag = abs(mag - self.prev_mag)
                                    self.prev_mag = mag

                                    now = time.time()
                                    if delta_mag > self.app.shake_threshold.get() and (now - self.last_shake) > SHAKE_COOLDOWN:
                                        self.last_shake = now
                                        self.app.log(f"⚡ Shake erkannt! (Stärke: {delta_mag:.1f})")
                                        ui.write(e.EV_KEY, e.BTN_WEST, 1)
                                        ui.syn()
                                        time.sleep(0.05)
                                        ui.write(e.EV_KEY, e.BTN_WEST, 0)
                                        
                                elif any(cap[0] == ev.code for cap in UINPUT_CAPS[e.EV_ABS]):
                                    ui.write(e.EV_ABS, ev.code, ev.value)

                            elif ev.type == e.EV_SYN: ui.syn()
                    except OSError: self.stop_ev.set()
        finally:
            ui.close()
            for d in devices:
                try: d.ungrab()
                except: pass
                d.close()
            sel.close()

    def stop(self): self.stop_ev.set()

class RenameInputsDialog(tk.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title("Eingaben umbenennen")
        self.geometry("450x380")
        if self.app.dark_mode.get(): self.configure(bg="#2d2d2d")

        ttk.Label(self, text="Eingaben-Bezeichnungen anpassen", font=("", 11, "bold")).pack(anchor="w", padx=10, pady=8)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.tree = ttk.Treeview(frame, columns=("code", "label"), show="headings", height=10)
        self.tree.heading("code", text="Original Code")
        self.tree.heading("label", text="Eigener Name")
        self.tree.column("code", width=180)
        self.tree.column("label", width=220)
        self.tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_frame, text="Hinzufügen / Bearbeiten", command=self._edit_entry).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Löschen", command=self._delete_entry).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Schließen", command=self.destroy).pack(side="right", padx=4)
        self._populate()

    def _populate(self):
        for item in self.tree.get_children(): self.tree.delete(item)
        for code, label in sorted(self.app.custom_labels.items()): self.tree.insert("", "end", values=(code, label))

    def _edit_entry(self):
        sel = self.tree.selection()
        d_code, d_label = (self.tree.item(sel[0])["values"][0], self.tree.item(sel[0])["values"][1]) if sel else ("", "")
        code = simpledialog.askstring("Code", "Code / Name:", initialvalue=d_code, parent=self)
        if not code: return
        label = simpledialog.askstring("Wunschname", f"Name für '{code.strip()}':", initialvalue=d_label, parent=self)
        if label is None: return
        if label.strip(): self.app.custom_labels[code.strip()] = label.strip()
        else: self.app.custom_labels.pop(code.strip(), None)
        self.app._save_labels(); self._populate()

    def _delete_entry(self):
        sel = self.tree.selection()
        if not sel: return
        code = self.tree.item(sel[0])["values"][0]
        if code in self.app.custom_labels:
            del self.app.custom_labels[code]
            self.app._save_labels(); self._populate()

class InputDebug(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Switchmote – Sensoren & Debug")
        self.active_mac, self.axes, self.pressed, self.job = None, {}, set(), None
        if self.app.dark_mode.get(): self.configure(bg="#2d2d2d")
        
        sf = ttk.LabelFrame(self, text="Bewegungs-Einstellungen")
        sf.pack(fill="x", padx=8, pady=6)
        
        ttk.Label(sf, text="Shake-Empfindlichkeit (Threshold):").grid(row=0, column=0, padx=8, pady=4, sticky="w")
        scale = ttk.Scale(sf, from_=10.0, to=150.0, orient="horizontal", variable=self.app.shake_threshold)
        scale.grid(row=0, column=1, padx=8, pady=4, sticky="ew")
        
        lbl_val = ttk.Label(sf, text="")
        lbl_val.grid(row=0, column=2, padx=8)
        
        def update_lbl(*args): 
            lbl_val.config(text=f"{self.app.shake_threshold.get():.1f}")
            self.app._save_settings_delayed()
            
        self.app.shake_threshold.trace_add("write", update_lbl)
        update_lbl()
        
        ttk.Button(sf, text="Nullpunkt Kalibrieren (10s)", command=self.app._calibrate_motion).grid(row=1, column=0, columnspan=3, pady=8)
        sf.columnconfigure(1, weight=1)

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        self.combo = ttk.Combobox(bar, state="readonly", width=30)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._start())
        ttk.Button(bar, text="Aktualisieren", command=self._scan).pack(side="left", padx=4)

        kf = ttk.LabelFrame(self, text="Gedrückte Tasten (Rohdaten)")
        kf.pack(fill="x", padx=8, pady=4)
        self.keys_var = tk.StringVar(value="–")
        ttk.Label(kf, textvariable=self.keys_var, font=("", 11, "bold")).pack(anchor="w", padx=8, pady=6)

        self.axes_frame = ttk.LabelFrame(self, text="Achsen / Bewegung")
        self.axes_frame.pack(fill="x", padx=8, pady=4)
        self.axes_frame.columnconfigure(2, weight=1)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self._scan(); self._poll()

    def _scan(self):
        vals, self.macs = [], []
        for mac, mapper in self.app.mappers.items():
            vals.append(f"Switchmote {mapper.slot} [{mac}]")
            self.macs.append(mac)
        self.combo["values"] = vals
        if vals:
            self.combo.current(0)
            self._start()
        else:
            self.combo.set("Keine aktiven Switchmotes gefunden.")
            self.active_mac = None

    def _start(self):
        i = self.combo.current()
        if 0 <= i < len(self.macs):
            self.active_mac = self.macs[i]
            for w in self.axes_frame.winfo_children(): w.destroy()
            self.axes.clear(); self.pressed.clear(); self.keys_var.set("–")
            while not self.app.debug_q.empty():
                try: self.app.debug_q.get_nowait()
                except queue.Empty: break

    def _poll(self):
        try:
            latest_abs, keys_changed = {}, False
            while True:
                try:
                    name, t, c, v = self.app.debug_q.get_nowait()
                    if t == e.EV_KEY:
                        key_str = resolve_ecode(evdev.ecodes.KEY, c, "KEY", self.app.custom_labels)
                        if v: self.pressed.add(key_str)
                        else: self.pressed.discard(key_str)
                        keys_changed = True
                    elif t == e.EV_ABS: latest_abs[(name, c)] = v
                except queue.Empty: break

            if keys_changed: self.keys_var.set(", ".join(sorted(self.pressed)) or "–")

            for (name, c), v in latest_abs.items():
                abs_str = resolve_ecode(evdev.ecodes.ABS, c, "ABS", self.app.custom_labels)
                key = (name, c)
                if key not in self.axes:
                    r = len(self.axes)
                    ttk.Label(self.axes_frame, text=f"{name.replace('Nintendo Wii Remote ', '')}: {abs_str}", width=30).grid(row=r, column=0, sticky="w", padx=4)
                    var = tk.StringVar()
                    ttk.Label(self.axes_frame, textvariable=var, width=6, anchor="e").grid(row=r, column=1)
                    bar = ttk.Progressbar(self.axes_frame, mode="determinate", maximum=255)
                    bar.grid(row=r, column=2, sticky="ew", padx=6, pady=2)
                    self.axes[key] = {"var": var, "bar": bar, "min": v, "max": v + 1}
                
                a = self.axes[key]
                a["min"] = min(a["min"], v)
                a["max"] = max(a["max"], v, a["min"] + 1)
                span = max(1, a["max"] - a["min"])
                a["var"].set(str(v))
                a["bar"].configure(maximum=span, value=v - a["min"])
        except Exception: pass
        finally: self.job = self.after(50, self._poll)

    def close(self):
        if self.job: self.after_cancel(self.job)
        self.app.dbg = None
        self.destroy()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Switchmote")
        font.nametofont("TkDefaultFont").configure(family="sans-serif", size=10)
        try: self.iconphoto(True, tk.PhotoImage(data=ICON_B64))
        except Exception: pass

        self.q = queue.Queue()
        self.debug_q = queue.Queue(maxsize=200)
        
        self.dark_mode = tk.BooleanVar(value=False)
        self.log_file_var = tk.BooleanVar(value=False)
        self.shake_threshold = tk.DoubleVar(value=45.0)
        self.offsets = {}
        self._save_timer = None
        
        self.slots, saved_dark, saved_log = self._load_settings()
        self.dark_mode.set(saved_dark)
        self.log_file_var.set(saved_log)

        self.custom_labels = self._load_labels()
        self.infos, self.found, self.mappers, self.prev_state = {}, {}, {}, {}
        self.busy = set()
        self.cards = []
        self.scan_proc = None
        self.refreshing = False
        self.dbg = None
        self.only_ctrl = tk.BooleanVar(value=True)

        self._build(); self._apply_theme(); self._render()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._pump)
        self.after(300, self._refresh)
        self._bg(lambda: btctl("power", "on", timeout=5))

    def log(self, msg):
        def _append():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", f"{msg}\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.q.put(_append)

    def _calibrate_motion(self):
        if not self.mappers:
            messagebox.showerror("Fehler", "Keine Wiimote verbunden.")
            return
        
        for mapper in self.mappers.values():
            mapper.start_calibration()
            
        win = tk.Toplevel(self)
        win.title("Kalibrierung")
        if self.dark_mode.get(): win.configure(bg="#2d2d2d")
        
        lbl = ttk.Label(win, text="Bitte die Wiimote flach hinlegen...\n(z.B. in der Mario-Kart Lenkrad Position)\n\nNicht bewegen!", justify="center")
        lbl.pack(padx=30, pady=20)
        
        countdown = tk.IntVar(value=10)
        lbl_count = ttk.Label(win, textvariable=countdown, font=("", 32, "bold"))
        lbl_count.pack(pady=10)
        
        def step():
            c = countdown.get()
            if c > 0:
                countdown.set(c - 1)
                self.after(1000, step)
            else:
                win.destroy()
                for mac, mapper in self.mappers.items():
                    off_rx, off_ry, off_rz = mapper.finish_calibration()
                    self.offsets[mac] = {"rx": off_rx, "ry": off_ry, "rz": off_rz}
                self._save_settings()
                messagebox.showinfo("Fertig", "Nullpunkt erfolgreich kalibriert und gespeichert.")
                
        self.after(1000, step)

    def _build(self):
        self.menubar = tk.Menu(self)
        self.debug_menu = tk.Menu(self.menubar, tearoff=0)
        self.debug_menu.add_checkbutton(label="Dunkelmodus (Dark Mode)", variable=self.dark_mode, command=self._toggle_dark_mode)
        self.debug_menu.add_checkbutton(label="Signal & Abbrüche in Datei loggen", variable=self.log_file_var, command=self._save_settings)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Eingaben umbenennen …", command=lambda: RenameInputsDialog(self, self))
        self.debug_menu.add_command(label="Sensoren & Debug …", command=self._open_debug)
        self.menubar.add_cascade(label="Debug / Einstellungen", menu=self.debug_menu)
        self.config(menu=self.menubar)

        top = ttk.LabelFrame(self, text=f"Controller-Slots (max. {MAX_SLOTS})")
        top.pack(fill="x", padx=8, pady=4)
        top.columnconfigure(0, weight=1); top.columnconfigure(1, weight=1)
        for i in range(MAX_SLOTS): self.cards.append(self._card(top, i))

        mid = ttk.LabelFrame(self, text="Geräte suchen")
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        bar = ttk.Frame(mid)
        bar.pack(fill="x", padx=8, pady=6)
        self.scan_btn = ttk.Button(bar, text="Scan starten", command=self._toggle_scan)
        self.scan_btn.pack(side="left")
        ttk.Checkbutton(bar, text="Nur Controller anzeigen", variable=self.only_ctrl, command=self._fill_list).pack(side="left", padx=12)
        ttk.Button(bar, text="Zuweisen + Verbinden", command=self._pair_selected).pack(side="right")

        lf = ttk.Frame(mid)
        lf.pack(fill="both", expand=True, padx=8)
        self.listbox = tk.Listbox(lf, height=5, exportselection=False)
        sb = ttk.Scrollbar(lf, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        lg = ttk.LabelFrame(self, text="System-Protokoll")
        lg.pack(fill="both", padx=8, pady=(4, 8))
        self.logbox = tk.Text(lg, height=7, state="disabled", wrap="word")
        ls = ttk.Scrollbar(lg, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        ls.pack(side="right", fill="y")

    def _card(self, parent, i):
        f = ttk.LabelFrame(parent, text=f"Switchmote {i + 1}")
        f.grid(row=i // 2, column=i % 2, sticky="nsew", padx=6, pady=6)
        c = {"name": tk.StringVar(), "status": tk.StringVar(), "mac": tk.StringVar()}
        ttk.Label(f, textvariable=c["name"], font=("", 10, "bold")).pack(anchor="w", padx=8, pady=(4, 0))
        c["lbl"] = ttk.Label(f, textvariable=c["status"])
        c["lbl"].pack(anchor="w", padx=8, pady=2)
        row = ttk.Frame(f)
        row.pack(anchor="w", padx=8, pady=(2, 6))
        c["btn"] = []
        for text, cmd in (("Verbinden", self._connect), ("Trennen", self._disconnect), ("Entfernen", self._remove)):
            b = ttk.Button(row, text=text, command=lambda i=i, cmd=cmd: cmd(i))
            b.pack(side="left", padx=(0, 4))
            c["btn"].append(b)
        return c

    def _toggle_dark_mode(self):
        self._apply_theme(); self._save_settings()

    def _apply_theme(self):
        is_dark = self.dark_mode.get()
        bg_color = "#2d2d2d" if is_dark else "#f0f0f0"
        fg_color = "#ffffff" if is_dark else "#000000"
        input_bg = "#1e1e1e" if is_dark else "#ffffff"
        btn_bg = "#3c3c3c" if is_dark else "#e1e1e1"
        select_bg = "#007acc" if is_dark else "#0078d7"

        self.configure(bg=bg_color)
        style = ttk.Style()
        if 'clam' in style.theme_names(): style.theme_use('clam')

        if is_dark:
            style.configure(".", background=bg_color, foreground=fg_color, fieldbackground=input_bg)
            style.configure("TFrame", background=bg_color)
            style.configure("TLabelframe", background=bg_color, foreground=fg_color)
            style.configure("TLabelframe.Label", background=bg_color, foreground=fg_color)
            style.configure("TLabel", background=bg_color, foreground=fg_color)
            style.configure("TButton", background=btn_bg, foreground=fg_color, borderwidth=1)
            style.map("TButton", background=[("active", "#505050"), ("disabled", "#2b2b2b")], foreground=[("disabled", "#777777")])
            style.configure("TCheckbutton", background=bg_color, foreground=fg_color)
            style.configure("TCombobox", fieldbackground=input_bg, background=btn_bg, foreground=fg_color)
            style.configure("TScale", background=bg_color)
            style.configure("Treeview", background=input_bg, foreground=fg_color, fieldbackground=input_bg)
            style.configure("Treeview.Heading", background=btn_bg, foreground=fg_color)
            style.map("Treeview", background=[("selected", select_bg)], foreground=[("selected", "#ffffff")])
            style.configure("TProgressbar", troughcolor=input_bg, background=select_bg)
        else:
            style.configure(".", background="#f0f0f0", foreground="#000000", fieldbackground="#ffffff")
            style.configure("TFrame", background="#f0f0f0")
            style.configure("TLabelframe", background="#f0f0f0", foreground="#000000")
            style.configure("TLabelframe.Label", background="#f0f0f0", foreground="#000000")
            style.configure("TLabel", background="#f0f0f0", foreground="#000000")
            style.configure("TButton", background="#e1e1e1", foreground="#000000", borderwidth=1)
            style.map("TButton", background=[("active", "#ececec"), ("disabled", "#f4f4f4")], foreground=[("disabled", "#a0a0a0")])
            style.configure("TCheckbutton", background="#f0f0f0", foreground="#000000")
            style.configure("TCombobox", fieldbackground="#ffffff", background="#e1e1e1", foreground="#000000")
            style.configure("TScale", background="#f0f0f0")
            style.configure("Treeview", background="#ffffff", foreground="#000000", fieldbackground="#ffffff")
            style.configure("Treeview.Heading", background="#e1e1e1", foreground="#000000")
            style.map("Treeview", background=[("selected", select_bg)], foreground=[("selected", "#ffffff")])
            style.configure("TProgressbar", troughcolor="#e6e6e6", background="#0078d7")

        if hasattr(self, 'menubar'):
            self.menubar.configure(bg=bg_color, fg=fg_color, activebackground=btn_bg, activeforeground=fg_color, bd=0)
            self.debug_menu.configure(bg=input_bg if is_dark else "#ffffff", fg=fg_color, activebackground=select_bg, activeforeground="#ffffff", bd=0)

        self.listbox.configure(bg=input_bg, fg=fg_color, selectbackground=select_bg, selectforeground="#ffffff")
        self.logbox.configure(bg=input_bg, fg=fg_color, insertbackground=fg_color)
        if self.dbg and self.dbg.winfo_exists(): self.dbg.configure(bg=bg_color)

    def _render(self):
        for i, c in enumerate(self.cards):
            mac = self.slots[i]
            if not mac:
                c["name"].set("— frei —"); c["status"].set("Gerät zuweisen"); c["lbl"].configure(foreground=GRAY)
                for b in c["btn"]: b.state(["disabled"])
                continue
            
            info = self.infos.get(mac)
            base_name = (info or {}).get("name") or mac
            
            extras = []
            if (info or {}).get("battery"): extras.append(f"🔋 {info['battery']}")
            if (info or {}).get("rssi"): extras.append(f"📶 {info['rssi']} dBm")
            name_display = f"{base_name}  [{' | '.join(extras)}]" if extras else base_name

            busy, connected = mac in self.busy, bool(info and info["connected"])
            if busy: text, color = "Bitte warten …", ORANGE
            elif info is None: text, color = "Lade Status …", GRAY
            elif connected: text, color = f"Verbunden [{mac}]", GREEN
            elif info["paired"]: text, color = f"Gekoppelt [{mac}]", ORANGE
            else: text, color = "Nicht gekoppelt", RED
            
            c["name"].set(name_display); c["status"].set(text); c["lbl"].configure(foreground=color)
            c["btn"][0].state(["disabled"] if busy or connected else ["!disabled"])
            c["btn"][1].state(["!disabled"] if connected and not busy else ["disabled"])
            c["btn"][2].state(["disabled"] if busy else ["!disabled"])

    def _fill_list(self):
        sel = self.listbox.curselection()
        sel_mac = self.devs[sel[0]][0] if sel and sel[0] < len(self.devs) else None
        self.devs = [(m, n) for m, n in sorted(self.found.items(), key=lambda x: x[1].lower()) 
                     if m not in self.slots and (not self.only_ctrl.get() or any(h in n.lower() for h in HINTS))]
        self.listbox.delete(0, "end")
        for k, (m, n) in enumerate(self.devs):
            self.listbox.insert("end", f"{n}   [{m}]")
            if m == sel_mac: self.listbox.selection_set(k)

    def _bg(self, fn): threading.Thread(target=fn, daemon=True).start()
    def _pump(self):
        while not self.q.empty(): self.q.get_nowait()()
        self.after(100, self._pump)

    def _load_settings(self):
        try:
            data = json.loads(CONFIG_FILE.read_text())
            if isinstance(data, dict):
                self.shake_threshold.set(data.get("shake_threshold", 45.0))
                self.offsets = data.get("offsets", {})
                return data.get("slots", [None]*MAX_SLOTS)[:MAX_SLOTS] + [None]*(MAX_SLOTS-len(data.get("slots", []))), data.get("dark_mode", False), data.get("log_signal", False)
            elif isinstance(data, list):
                return data[:MAX_SLOTS] + [None]*(MAX_SLOTS-len(data)), False, False
        except Exception: pass
        return [None] * MAX_SLOTS, False, False

    def _save_settings_delayed(self):
        if self._save_timer: self.after_cancel(self._save_timer)
        self._save_timer = self.after(1000, self._save_settings)

    def _save_settings(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({
                "slots": self.slots[:MAX_SLOTS], 
                "dark_mode": self.dark_mode.get(), 
                "log_signal": self.log_file_var.get(),
                "shake_threshold": self.shake_threshold.get(),
                "offsets": self.offsets
            }, indent=2))
        except OSError: pass

    def _load_labels(self):
        try: return json.loads(LABELS_FILE.read_text())
        except Exception: return {}

    def _save_labels(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            LABELS_FILE.write_text(json.dumps(self.custom_labels, indent=2))
        except OSError: pass

    def _log_to_file(self, mac, event, rssi):
        if not self.log_file_var.get(): return
        try:
            if not LOG_FILE.exists(): LOG_FILE.write_text("Zeitstempel,MAC,Ereignis,RSSI\n")
            with open(LOG_FILE, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{mac},{event},{rssi or ''}\n")
        except Exception: pass

    def _refresh(self):
        if not self.refreshing:
            self.refreshing = True
            macs = [m for m in self.slots if m]
            def work():
                infos = {m: parse_info(btctl("info", m, timeout=5), m) for m in macs}
                devs = parse_devices(btctl("devices", timeout=5)) if self.scan_proc else None
                self.q.put(lambda: self._apply(infos, devs))
            self._bg(work)
        self.after(3000, self._refresh)

    def _apply(self, infos, devs):
        self.refreshing = False
        self.infos = infos
        if devs is not None:
            self.found = devs; self._fill_list()
        self._render()

        needs_scan = any(infos.get(mac, {}).get("connected") and mac not in self.mappers for mac in self.slots if mac)
        mac_groups = get_grouped_devices() if needs_scan else {}
        all_nin_paths = [p for p in evdev.list_devices() if any(h in (evdev.InputDevice(p).name or "").lower() for h in ("nintendo", "wii", "joy-con"))] if needs_scan and not mac_groups else []

        for i, mac in enumerate(self.slots):
            if not mac: continue
            slot_num = i + 1
            info = infos.get(mac, {})
            is_connected = info.get("connected", False)
            rssi = info.get("rssi")

            prev = self.prev_state.get(mac, {"connected": False, "rssi": None})
            was_connected = prev.get("connected", False)

            if is_connected and not was_connected:
                self.log(f"🟢 Switchmote {slot_num} verbunden ({mac}).")
                self._log_to_file(mac, "VERBUNDEN", rssi)
            elif not is_connected and was_connected:
                self.log(f"🔴 Switchmote {slot_num} VERBINDUNG ABGEBROCHEN ({mac})!")
                self._log_to_file(mac, "ABBRUCH", None)
            elif is_connected and self.log_file_var.get() and rssi and rssi != prev.get("rssi"):
                self._log_to_file(mac, "SIGNAL_UPDATE", rssi)

            self.prev_state[mac] = {"connected": is_connected, "rssi": rssi}

            if is_connected:
                if mac not in self.mappers and needs_scan:
                    paths = mac_groups.get(mac, all_nin_paths)
                    if paths:
                        m = SwitchmoteMapper(self, slot_num, mac, paths)
                        m.start()
                        self.mappers[mac] = m
                        self.log(f"Virtuelles XInput-Gerät 'Switchmote {slot_num}' gestartet.")
            elif mac in self.mappers:
                self.mappers[mac].stop()
                del self.mappers[mac]
                self.log(f"'Switchmote {slot_num}' beendet.")

    def _toggle_scan(self):
        if self.scan_proc:
            self.scan_proc.terminate(); self.scan_proc = None
            self.scan_btn.configure(text="Scan starten")
        else:
            self.scan_proc = subprocess.Popen(["bluetoothctl", "--timeout", "30", "scan", "on"], stdout=subprocess.DEVNULL)
            self.scan_btn.configure(text="Scan stoppen")
            self.log("Suche Geräte …")
            self._bg(lambda: (self.scan_proc.wait(), self.q.put(lambda: self._toggle_scan() if self.scan_proc else None)))

    def _pair_selected(self):
        sel = self.listbox.curselection()
        if not sel: return messagebox.showinfo("Hinweis", "Bitte Gerät auswählen.")
        if None not in self.slots: return messagebox.showwarning("Voll", "Bitte erst einen Slot leeren.")
        mac = self.devs[sel[0]][0]
        self.slots[self.slots.index(None)] = mac
        self._save_settings()
        if self.scan_proc: self._toggle_scan()
        self._fill_list(); self._render()
        
        def work():
            self.busy.add(mac); self.q.put(self._render)
            self.log(f"=== {mac} Zuweisen ===")
            if not parse_info(btctl("info", mac, timeout=10), mac)["paired"]:
                out = btctl("--agent", "NoInputNoOutput", "pair", mac, timeout=30)
                if "Pairing successful" not in out and "AlreadyExists" not in out:
                    self.log("Koppeln fehlgeschlagen."); self.busy.discard(mac); self.q.put(self._render); return
            self.log(btctl("trust", mac, timeout=5))
            self.log(btctl("connect", mac, timeout=10))
            self.busy.discard(mac); self.q.put(self._render)
        self._bg(work)

    def _connect(self, i):
        mac = self.slots[i]
        def work():
            self.busy.add(mac); self.q.put(self._render)
            self.log(f"Verbinde {mac}…")
            self.log("Erfolg!" if "successful" in btctl("connect", mac, timeout=10) else "Fehlgeschlagen.")
            self.busy.discard(mac); self.q.put(self._render)
        self._bg(work)

    def _disconnect(self, i):
        mac = self.slots[i]
        def work():
            self.busy.add(mac); self.q.put(self._render)
            self.log(btctl("disconnect", mac, timeout=8))
            self.busy.discard(mac); self.q.put(self._render)
        self._bg(work)

    def _remove(self, i):
        mac = self.slots[i]
        if messagebox.askyesno("Entfernen", f"Switchmote {i+1} freigeben?"):
            self.slots[i] = None; self._save_settings(); self._render()
            self._bg(lambda: btctl("remove", mac, timeout=8))

    def _open_debug(self):
        if self.dbg and self.dbg.winfo_exists(): self.dbg.lift()
        else: self.dbg = InputDebug(self)

    def _close(self):
        if self.scan_proc: self.scan_proc.terminate()
        for m in self.mappers.values(): m.stop()
        if self.dbg: self.dbg.close()
        self.destroy()

if __name__ == "__main__":
    if not shutil.which("bluetoothctl"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Fehler", "BlueZ/bluetoothctl fehlt.")
    else: App().mainloop()