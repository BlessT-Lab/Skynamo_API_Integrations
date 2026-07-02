"""
Skynamo Customer Geolocation Updater - desktop GUI
==================================================
A CustomTkinter front-end over skynamo_geo.engine. Flow:

  Connect & Load  ->  Map address fields  ->  Preview (geocode only)
  ->  review/deselect rows  ->  Write Selected to Skynamo  ->  Save report

The engine does all the work on a background thread; the UI stays responsive
and progress is streamed back through a thread-safe queue. Tkinter widgets are
only ever touched on the main thread (via _poll_queue).
"""

import queue
import threading
from datetime import datetime
from tkinter import ttk, filedialog

import customtkinter as ctk

from skynamo_geo import engine, settings
from skynamo_geo.client import SkynamoClient
from skynamo_geo.config import (
    STATUS_UPDATED, STATUS_UPDATED_LOW_CONF, STATUS_SKIPPED_HAS_COORDS,
    STATUS_SKIPPED_NO_ADDRESS, STATUS_GEOCODE_FAILED, STATUS_UPDATE_FAILED,
    STATUS_PENDING, GEOCODER_PROVIDERS, DEFAULT_PROVIDER,
)
from skynamo_geo.customers import build_address, collect_custom_field_names
from skynamo_geo.geocoder import create_geocoder, GeocodeError

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ----- Palette (base background is rgb(26,26,26)) -----
BG = "#1a1a1a"            # window background
CARD = "#232323"          # panel/card surfaces
FIELD = "#2b2b2b"         # entry/input surfaces
BORDER = "#3a3a3a"
ACCENT = "#4f8cff"
ACCENT_HOVER = "#3b74e0"
GREEN = "#2ea36b"
GREEN_HOVER = "#238053"
RED = "#d64545"
RED_HOVER = "#aa3535"
TEXT = "#e8e8e8"
TEXT_MUTED = "#9a9a9a"

CHECK_ON = "☑"   # ballot box with check
CHECK_OFF = "☐"  # empty ballot box

PROVIDER_LABELS = list(GEOCODER_PROVIDERS.values())
PROVIDER_BY_LABEL = {label: key for key, label in GEOCODER_PROVIDERS.items()}


class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("Skynamo Geolocation Updater")
        self.geometry("1080x780")
        self.minsize(940, 660)

        # Runtime state
        self.client = None
        self.geocoder = None
        self.country = None
        self.customers = []
        self.field_vars = {}     # field name -> BooleanVar
        self.plans = []
        self.report_rows = []
        self.tree_item_to_plan = {}  # tree iid -> Plan

        # Threading
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None

        self._build_ui()
        self._load_saved_settings()

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._style_treeview()

        # ----- Header -----
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(14, 4), sticky="ew")
        ctk.CTkLabel(header, text="Skynamo Geolocation Updater",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(header,
                     text="geocode customer addresses · preview · commit",
                     font=ctk.CTkFont(size=12),
                     text_color=TEXT_MUTED).pack(side="left", padx=(12, 0),
                                                 pady=(4, 0))

        # ----- Top: connection + mapping side by side -----
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, padx=16, pady=(6, 6), sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        # Connection panel
        conn = self._card(top)
        conn.grid(row=0, column=0, padx=(0, 8), pady=0, sticky="nsew")
        self._section_title(conn, "1", "Connection").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(12, 6), sticky="w")

        self.instance_entry = self._labeled_entry(conn, "Instance name", 1)
        self.skynamo_entry = self._labeled_entry(conn, "Skynamo API key", 2,
                                                 show="*")

        ctk.CTkLabel(conn, text="Geocoding provider", anchor="w",
                     text_color=TEXT).grid(
            row=3, column=0, padx=(14, 6), pady=4, sticky="w")
        self.provider_seg = ctk.CTkSegmentedButton(
            conn, values=PROVIDER_LABELS, command=self._on_provider_change,
            fg_color=FIELD, selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER,
            unselected_color=FIELD, unselected_hover_color=BORDER,
            corner_radius=8, height=30)
        self.provider_seg.set(GEOCODER_PROVIDERS[DEFAULT_PROVIDER])
        self.provider_seg.grid(row=3, column=1, padx=(0, 14), pady=4,
                               sticky="ew")

        self.google_label = ctk.CTkLabel(conn, text="Google Maps API key",
                                         anchor="w", text_color=TEXT)
        self.google_label.grid(row=4, column=0, padx=(14, 6), pady=4,
                               sticky="w")
        self.google_entry = self._entry(conn, show="*")
        self.google_entry.grid(row=4, column=1, padx=(0, 14), pady=4,
                               sticky="ew")

        self.country_entry = self._labeled_entry(
            conn, "Country (2-letter, optional)", 5)

        self.remember_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(conn, text="Remember credentials & settings",
                        variable=self.remember_var,
                        checkbox_width=20, checkbox_height=20,
                        corner_radius=5, border_color=BORDER,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        text_color=TEXT).grid(
            row=6, column=0, columnspan=2, padx=14, pady=6, sticky="w")

        self.connect_btn = self._button(
            conn, "Connect & Load Customers", self.on_connect)
        self.connect_btn.grid(row=7, column=0, columnspan=2,
                              padx=14, pady=(6, 14), sticky="ew")

        # Mapping panel
        mapping = self._card(top)
        mapping.grid(row=0, column=1, padx=(8, 0), pady=0, sticky="nsew")
        mapping.grid_rowconfigure(1, weight=1)
        mapping.grid_columnconfigure(0, weight=1)
        self._section_title(mapping, "2", "Map address field(s)").grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")

        self.fields_frame = ctk.CTkScrollableFrame(
            mapping, height=150, fg_color=FIELD, corner_radius=10)
        self.fields_frame.grid(row=1, column=0, padx=14, pady=4, sticky="nsew")
        self._fields_placeholder()

        self.sample_label = ctk.CTkLabel(
            mapping, text="Sample address: -", anchor="w",
            wraplength=460, justify="left", text_color=TEXT_MUTED)
        self.sample_label.grid(row=2, column=0, padx=14, pady=2, sticky="ew")

        self.replace_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(mapping,
                        text="Replace coordinates that already exist",
                        variable=self.replace_var,
                        checkbox_width=20, checkbox_height=20,
                        corner_radius=5, border_color=BORDER,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        text_color=TEXT).grid(
            row=3, column=0, padx=14, pady=(4, 14), sticky="w")

        # ----- Middle: action bar -----
        actions = self._card(self)
        actions.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        self.preview_btn = self._button(
            actions, "Preview (geocode only)", self.on_preview,
            state="disabled")
        self.preview_btn.pack(side="left", padx=(12, 6), pady=10)
        self.write_btn = self._button(
            actions, "Write Selected to Skynamo", self.on_write,
            state="disabled", fg_color=GREEN, hover_color=GREEN_HOVER)
        self.write_btn.pack(side="left", padx=6, pady=10)
        self.cancel_btn = self._button(
            actions, "Cancel", self.on_cancel, state="disabled",
            fg_color=RED, hover_color=RED_HOVER, width=90)
        self.cancel_btn.pack(side="left", padx=6, pady=10)
        self.save_btn = self._button(
            actions, "Save Report CSV", self.on_save_report,
            state="disabled", fg_color=FIELD, hover_color=BORDER)
        self.save_btn.pack(side="left", padx=6, pady=10)

        self.select_all_btn = self._button(
            actions, "Select all", lambda: self._set_all_includes(True),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.select_all_btn.pack(side="right", padx=(6, 12), pady=10)
        self.select_none_btn = self._button(
            actions, "Select none", lambda: self._set_all_includes(False),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.select_none_btn.pack(side="right", padx=6, pady=10)

        # ----- Results table -----
        table_frame = self._card(self)
        table_frame.grid(row=3, column=0, padx=16, pady=6, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        columns = ("include", "name", "address", "lat", "lng",
                   "precision", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns,
                                 show="headings", style="Dark.Treeview")
        headings = {
            "include": ("Use", 50), "name": ("Customer", 200),
            "address": ("Address used", 280), "lat": ("Latitude", 90),
            "lng": ("Longitude", 90), "precision": ("Precision", 130),
            "status": ("Status", 150),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            anchor = "center" if col in ("include", "lat", "lng") else "w"
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.tag_configure("low", background="#3a2f12",
                                foreground="#f0c453")
        self.tree.tag_configure("skip", foreground="#7a7a7a")
        self.tree.tag_configure("fail", background="#3a1717",
                                foreground="#f08c8c")
        self.tree.grid(row=0, column=0, padx=(10, 0), pady=10, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical",
                                command=self.tree.yview,
                                style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="ns")
        self.tree.bind("<Button-1>", self._on_tree_click)

        # ----- Bottom: progress + log + summary -----
        bottom = self._card(self)
        bottom.grid(row=4, column=0, padx=16, pady=(6, 14), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(
            bottom, progress_color=ACCENT, fg_color=FIELD, height=8,
            corner_radius=4)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, padx=14, pady=(12, 4), sticky="ew")
        self.status_label = ctk.CTkLabel(bottom, text="Ready.", anchor="w",
                                         text_color=TEXT)
        self.status_label.grid(row=1, column=0, padx=14, pady=2, sticky="ew")
        self.log = ctk.CTkTextbox(
            bottom, height=110, fg_color="#151515", text_color="#b8b8b8",
            corner_radius=10, border_width=1, border_color=BORDER,
            font=ctk.CTkFont(family="Consolas", size=12))
        self.log.grid(row=2, column=0, padx=14, pady=(4, 14), sticky="ew")

    # -- Styled widget helpers --------------------------------------------

    def _card(self, parent):
        return ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14,
                            border_width=1, border_color="#2e2e2e")

    def _section_title(self, parent, number, text):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(frame, text=number, width=24, height=24,
                     fg_color=ACCENT, corner_radius=12, text_color="#ffffff",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(
            side="left")
        ctk.CTkLabel(frame, text=text, text_color=TEXT,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(
            side="left", padx=(8, 0))
        return frame

    def _entry(self, parent, show=None):
        return ctk.CTkEntry(parent, show=show, width=260, height=30,
                            corner_radius=8, fg_color=FIELD,
                            border_color=BORDER, border_width=1,
                            text_color=TEXT)

    def _button(self, parent, text, command, state="normal", width=None,
                fg_color=ACCENT, hover_color=ACCENT_HOVER):
        kwargs = {"width": width} if width else {}
        return ctk.CTkButton(parent, text=text, command=command, state=state,
                             height=34, corner_radius=8, fg_color=fg_color,
                             hover_color=hover_color, text_color=TEXT,
                             font=ctk.CTkFont(size=13, weight="bold"),
                             **kwargs)

    def _labeled_entry(self, parent, label, row, show=None):
        ctk.CTkLabel(parent, text=label, anchor="w", text_color=TEXT).grid(
            row=row, column=0, padx=(14, 6), pady=4, sticky="w")
        entry = self._entry(parent, show=show)
        entry.grid(row=row, column=1, padx=(0, 14), pady=4, sticky="ew")
        parent.grid_columnconfigure(1, weight=1)
        return entry

    def _style_treeview(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.Treeview", background="#202020",
                        fieldbackground="#202020", foreground=TEXT,
                        borderwidth=0, rowheight=26,
                        font=("Segoe UI", 10))
        # Drop clam's light outer border - keep only the tree area.
        style.layout("Dark.Treeview",
                     [("Dark.Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Dark.Treeview.Heading", background=FIELD,
                        foreground=TEXT, relief="flat", padding=(8, 6),
                        borderwidth=0, font=("Segoe UI", 10, "bold"))
        style.map("Dark.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#ffffff")])
        style.map("Dark.Treeview.Heading",
                  background=[("active", BORDER)])
        style.configure("Dark.Vertical.TScrollbar", background=FIELD,
                        troughcolor="#202020", borderwidth=0,
                        arrowcolor=TEXT_MUTED)
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", BORDER)])

    def _fields_placeholder(self):
        for child in self.fields_frame.winfo_children():
            child.destroy()
        ctk.CTkLabel(self.fields_frame,
                     text="Connect & load customers to list fields.",
                     text_color=TEXT_MUTED).pack(anchor="w", padx=8, pady=8)

    # -- Provider selection ------------------------------------------------

    def _provider_key(self):
        return PROVIDER_BY_LABEL.get(self.provider_seg.get(),
                                     DEFAULT_PROVIDER)

    def _on_provider_change(self, _value=None):
        if self._provider_key() == "google":
            self.google_entry.configure(state="normal")
            self.google_label.configure(text_color=TEXT)
        else:
            self.google_entry.configure(state="disabled")
            self.google_label.configure(text_color=TEXT_MUTED)

    # -- Logging / status -------------------------------------------------

    def log_line(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def set_status(self, text):
        self.status_label.configure(text=text)

    # -- Settings persistence --------------------------------------------

    def _load_saved_settings(self):
        cfg = settings.load_config()
        if cfg.get("instance_name"):
            self.instance_entry.insert(0, cfg["instance_name"])
        if cfg.get("country"):
            self.country_entry.insert(0, cfg["country"])
        self.replace_var.set(bool(cfg.get("replace_existing", False)))
        self._saved_fields = cfg.get("address_fields", [])
        provider = cfg.get("provider", DEFAULT_PROVIDER)
        if provider in GEOCODER_PROVIDERS:
            self.provider_seg.set(GEOCODER_PROVIDERS[provider])
        # Secrets
        google = settings.get_secret(settings.GOOGLE_KEY_NAME)
        if google:
            self.google_entry.insert(0, google)
        if cfg.get("instance_name"):
            sk = settings.get_secret(
                settings.skynamo_key_name(cfg["instance_name"]))
            if sk:
                self.skynamo_entry.insert(0, sk)
        self._on_provider_change()

    def _persist_settings(self):
        if not self.remember_var.get():
            return
        instance = self.instance_entry.get().strip()
        selected = [name for name, var in self.field_vars.items() if var.get()]
        settings.save_config({
            "instance_name": instance,
            "country": self.country_entry.get().strip().upper(),
            "replace_existing": self.replace_var.get(),
            "address_fields": selected,
            "provider": self._provider_key(),
        })
        settings.set_secret(settings.GOOGLE_KEY_NAME,
                            self.google_entry.get().strip())
        settings.set_secret(settings.skynamo_key_name(instance),
                            self.skynamo_entry.get().strip())

    # -- Worker plumbing --------------------------------------------------

    def _start_worker(self, target):
        self.cancel_event.clear()
        self._set_busy(True)
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        self.after(100, self._poll_queue)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    self._on_progress(payload)
                elif kind == "log":
                    self.log_line(payload)
                elif kind == "status":
                    self.set_status(payload)
                elif kind == "done":
                    payload()  # a callable that updates UI on main thread
                    self._set_busy(False)
                    return
                elif kind == "error":
                    self.log_line(f"ERROR: {payload}")
                    self.set_status("Error - see log.")
                    self._set_busy(False)
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_progress(self, ev):
        if ev["total"]:
            self.progress.set(ev["index"] / ev["total"])
        self.set_status(f"{ev['phase'].title()} {ev['index']}/{ev['total']}: "
                        f"{ev['name']}")

    def _set_busy(self, busy):
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        for btn in (self.connect_btn, self.preview_btn, self.write_btn,
                    self.save_btn, self.select_all_btn, self.select_none_btn):
            btn.configure(state="disabled" if busy else btn.cget("state"))
        if not busy:
            # Re-enable based on current state
            self.connect_btn.configure(state="normal")
            if self.customers:
                self.preview_btn.configure(state="normal")
            if self.plans:
                self.write_btn.configure(state="normal")
                self.select_all_btn.configure(state="normal")
                self.select_none_btn.configure(state="normal")
            if self.report_rows:
                self.save_btn.configure(state="normal")

    # -- Step 1: connect & load ------------------------------------------

    def on_connect(self):
        instance = self.instance_entry.get().strip()
        skynamo_key = self.skynamo_entry.get().strip()
        provider = self._provider_key()
        google_key = self.google_entry.get().strip()
        country = self.country_entry.get().strip().upper()
        if not (instance and skynamo_key):
            self.set_status("Enter instance name and Skynamo key first.")
            return
        if provider == "google" and not google_key:
            self.set_status("Enter a Google Maps API key, or switch the "
                            "provider to OpenStreetMap.")
            return
        if country and len(country) != 2:
            self.set_status("Country must be a 2-letter code (e.g. ZA) or blank.")
            return
        self.country = country or None
        provider_label = GEOCODER_PROVIDERS[provider]
        self.log_line(f"Connecting to '{instance}' "
                      f"(geocoder: {provider_label})...")

        def work():
            try:
                client = SkynamoClient(instance, skynamo_key)
                ok, message = client.test_connection()
                if not ok:
                    self.queue.put(("error", message))
                    return
                self.queue.put(("log", "Skynamo credentials OK."))
                geocoder = create_geocoder(provider, google_key or None)
                self.queue.put(("status",
                                f"Validating {provider_label} geocoder..."))
                geocoder.validate(country=self.country)
                self.queue.put(("log", f"{provider_label} geocoder OK."))
                self.queue.put(("status", "Fetching customers..."))
                customers = client.fetch_all_customers(
                    on_page=lambda n, total: self.queue.put((
                        "status", f"Fetched {n}"
                        f"{f' of {total}' if total else ''} customers...")))

                def finish():
                    self.client = client
                    self.geocoder = geocoder
                    self.customers = customers
                    self.log_line(f"Loaded {len(customers)} customers.")
                    self._populate_fields()
                    self.set_status(f"Loaded {len(customers)} customers. "
                                    f"Map fields, then Preview.")
                    self._persist_settings()
                self.queue.put(("done", finish))
            except GeocodeError as exc:
                self.queue.put(("error", str(exc)))
            except Exception as exc:
                self.queue.put(("error", str(exc)))

        self._start_worker(work)

    def _populate_fields(self):
        for child in self.fields_frame.winfo_children():
            child.destroy()
        self.field_vars = {}
        names = collect_custom_field_names(self.customers)
        if not names:
            ctk.CTkLabel(self.fields_frame,
                         text="No custom fields found on customers.",
                         text_color=TEXT_MUTED).pack(anchor="w", padx=8,
                                                     pady=8)
            return
        saved = set(getattr(self, "_saved_fields", []) or [])
        for name in names:
            var = ctk.BooleanVar(value=name in saved)
            var.trace_add("write", lambda *_: self._update_sample())
            ctk.CTkCheckBox(self.fields_frame, text=name, variable=var,
                            checkbox_width=18, checkbox_height=18,
                            corner_radius=4, border_color=BORDER,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            text_color=TEXT).pack(anchor="w", padx=8, pady=3)
            self.field_vars[name] = var
        self._update_sample()

    def _selected_fields(self):
        return [name for name, var in self.field_vars.items() if var.get()]

    def _update_sample(self):
        fields = self._selected_fields()
        if not fields:
            self.sample_label.configure(text="Sample address: -")
            return
        sample = next((build_address(c, fields) for c in self.customers
                       if build_address(c, fields)), None)
        self.sample_label.configure(
            text=f"Sample address: {sample or '(no customer has these fields filled)'}")

    # -- Step 2: preview (geocode only) ----------------------------------

    def on_preview(self):
        fields = self._selected_fields()
        if not fields:
            self.set_status("Select at least one address field.")
            return
        replace = self.replace_var.get()
        country = self.country
        self.plans = []
        self.report_rows = []
        self.save_btn.configure(state="disabled")
        self._clear_tree()
        self.log_line(f"Geocoding with fields: {' + '.join(fields)}")
        self._persist_settings()

        def work():
            try:
                plans = engine.geocode_customers(
                    self.geocoder, self.customers, fields,
                    replace_existing=replace, country=country,
                    on_progress=lambda ev: self.queue.put(("progress", ev)),
                    should_cancel=self.cancel_event.is_set)

                def finish():
                    self.plans = plans
                    self._populate_tree(plans)
                    counts = engine.summarize(plans)
                    self.set_status(self._summary_text(
                        counts, preview=True))
                    self.log_line("Preview complete. Review rows, then "
                                  "'Write Selected to Skynamo'.")
                self.queue.put(("done", finish))
            except GeocodeError as exc:
                self.queue.put(("error", str(exc)))
            except Exception as exc:
                self.queue.put(("error", str(exc)))

        self._start_worker(work)

    # -- Step 3: write to Skynamo ----------------------------------------

    def on_write(self):
        to_write = [p for p in self.plans if p.include and p.writable]
        if not to_write:
            self.set_status("No rows selected to write.")
            return
        self.log_line(f"Writing {len(to_write)} locations to Skynamo...")

        def work():
            try:
                report_rows = engine.write_locations(
                    self.client, self.plans,
                    on_progress=lambda ev: self.queue.put(("progress", ev)),
                    should_cancel=self.cancel_event.is_set)

                def finish():
                    self.report_rows = report_rows
                    self._refresh_tree_statuses()
                    counts = engine.summarize(self.plans)
                    self.set_status(self._summary_text(counts, preview=False))
                    self.log_line("Write complete. Save the report if needed.")
                    self.save_btn.configure(state="normal")
                self.queue.put(("done", finish))
            except Exception as exc:
                self.queue.put(("error", str(exc)))

        self._start_worker(work)

    def on_cancel(self):
        self.cancel_event.set()
        self.set_status("Cancelling...")
        self.log_line("Cancel requested - stopping after current item.")

    # -- Step 4: save report ---------------------------------------------

    def on_save_report(self):
        rows = self.report_rows or [p.to_report_row() for p in self.plans]
        if not rows:
            self.set_status("Nothing to save yet.")
            return
        default = (f"geolocation_report_"
                   f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        engine.write_report(rows, path)
        self.log_line(f"Report saved to: {path}")
        self.set_status(f"Report saved to {path}")

    # -- Tree helpers -----------------------------------------------------

    def _clear_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.tree_item_to_plan = {}

    def _row_tag(self, plan):
        if plan.status in (STATUS_SKIPPED_HAS_COORDS, STATUS_SKIPPED_NO_ADDRESS):
            return "skip"
        if plan.status in (STATUS_GEOCODE_FAILED, STATUS_UPDATE_FAILED):
            return "fail"
        if plan.low_confidence:
            return "low"
        return ""

    def _plan_values(self, plan):
        check = CHECK_ON if (plan.include and plan.writable) else CHECK_OFF
        if not plan.writable:
            check = ""  # nothing to write for skips/failures
        lat = f"{plan.lat:.6f}" if plan.result else ""
        lng = f"{plan.lng:.6f}" if plan.result else ""
        return (check, plan.name, plan.address, lat, lng,
                plan.precision, plan.status)

    def _populate_tree(self, plans):
        self._clear_tree()
        for plan in plans:
            tag = self._row_tag(plan)
            iid = self.tree.insert("", "end", values=self._plan_values(plan),
                                   tags=(tag,) if tag else ())
            self.tree_item_to_plan[iid] = plan

    def _refresh_tree_statuses(self):
        for iid, plan in self.tree_item_to_plan.items():
            tag = self._row_tag(plan)
            self.tree.item(iid, values=self._plan_values(plan),
                           tags=(tag,) if tag else ())

    def _on_tree_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":  # only the Use column
            return
        iid = self.tree.identify_row(event.y)
        plan = self.tree_item_to_plan.get(iid)
        if not plan or not plan.writable:
            return
        plan.include = not plan.include
        self.tree.set(iid, "include",
                      CHECK_ON if plan.include else CHECK_OFF)

    def _set_all_includes(self, value):
        for iid, plan in self.tree_item_to_plan.items():
            if plan.writable:
                plan.include = value
                self.tree.set(iid, "include",
                              CHECK_ON if value else CHECK_OFF)

    def _summary_text(self, counts, preview):
        parts = [
            f"precise={counts.get(STATUS_UPDATED, 0) + (counts.get(STATUS_PENDING, 0) if preview else 0)}",
            f"low-conf={counts.get(STATUS_UPDATED_LOW_CONF, 0)}",
            f"has-coords={counts.get(STATUS_SKIPPED_HAS_COORDS, 0)}",
            f"no-address={counts.get(STATUS_SKIPPED_NO_ADDRESS, 0)}",
            f"geocode-fail={counts.get(STATUS_GEOCODE_FAILED, 0)}",
        ]
        if not preview:
            parts.append(f"write-fail={counts.get(STATUS_UPDATE_FAILED, 0)}")
        label = "Preview" if preview else "Done"
        return f"{label}:  " + "   ".join(parts)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
