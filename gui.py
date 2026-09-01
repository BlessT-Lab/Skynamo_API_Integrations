"""
Skynamo Toolkit - desktop GUI
=============================
A CustomTkinter front-end over the skynamo_geo core, with four tabs:

  Customer Geolocation - geocode customer addresses and write coordinates.
  Product Images       - match local image files to products by code and upload.
  Manage Images        - view/remove the images already attached to a product.
  Reporting            - extract business data from the Reporting API into a
                         local store (a different API: OAuth2, read-only).

Each tab does its work on a background thread; the UI stays responsive and
progress is streamed back through a thread-safe queue. Tkinter widgets are only
ever touched on the main thread (via each tab's queue-poll loop).
"""

import os
import queue
import threading
import webbrowser
from datetime import datetime
from tkinter import ttk, filedialog

import customtkinter as ctk

from skynamo_geo import engine, image_engine, report_engine, settings
from skynamo_geo.client import SkynamoClient
from skynamo_geo.dashboard import build_dashboard
from skynamo_geo.report_store import ReportStore, default_store_path
from skynamo_geo.reporting_client import ReportingClient
from skynamo_geo.reporting_config import (
    DEFAULT_REPORTING_PERIOD, PERIOD_GROUPS, REPORTING_ENTITIES,
    REPORTING_REPORT_FIELDNAMES, STATUS_RPT_EXTRACTED, STATUS_RPT_FAILED,
    STATUS_RPT_PENDING, STATUS_RPT_SKIPPED,
)
from skynamo_geo.config import (
    IMAGE_FOLDER_FAILED, IMAGE_FOLDER_SUCCESS,
    STATUS_UPDATED, STATUS_UPDATED_LOW_CONF, STATUS_SKIPPED_HAS_COORDS,
    STATUS_SKIPPED_NO_ADDRESS, STATUS_GEOCODE_FAILED, STATUS_UPDATE_FAILED,
    STATUS_PENDING, ADDRESS_ROLES, ADDRESS_ROLE_LABELS, DEFAULT_ROLE,
    STATUS_IMG_PENDING, STATUS_IMG_UPLOADED, STATUS_IMG_NO_MATCH,
    STATUS_IMG_BAD_FORMAT, STATUS_IMG_AMBIGUOUS, STATUS_IMG_UPLOAD_FAILED,
    STATUS_ATT_LOADED, STATUS_ATT_FETCH_FAILED, STATUS_ATT_DELETED,
    STATUS_ATT_DELETE_FAILED, ATTACHED_IMAGE_REPORT_FIELDNAMES,
)
from skynamo_geo.customers import build_query, collect_custom_field_names
from skynamo_geo.products import product_code as product_code_of
from skynamo_geo.geocoder import NominatimGeocoder, GeocodeError

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

ROLE_LABELS = [ADDRESS_ROLE_LABELS[r] for r in ADDRESS_ROLES]
ROLE_BY_LABEL = {ADDRESS_ROLE_LABELS[r]: r for r in ADDRESS_ROLES}
ROLE_LABEL_BY_KEY = {r: ADDRESS_ROLE_LABELS[r] for r in ADDRESS_ROLES}

# Reporting periods, shown grouped ("Rolling - Prev30Days") so the 21 values
# stay navigable in one dropdown.
PERIOD_LABELS = [f"{group} - {period}"
                 for group, periods in PERIOD_GROUPS for period in periods]
PERIOD_BY_LABEL = {f"{group} - {period}": period
                   for group, periods in PERIOD_GROUPS for period in periods}
PERIOD_LABEL_BY_KEY = {v: k for k, v in PERIOD_BY_LABEL.items()}


class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("Skynamo Toolkit")
        self.geometry("1080x800")
        self.minsize(940, 680)

        # ----- Geolocation tab state -----
        self.client = None
        self.geocoder = None
        self.country = None
        self.customers = []
        self.field_vars = {}       # field name -> BooleanVar (selected?)
        self.field_role_vars = {}  # field name -> StringVar (role label)
        self.field_role_menus = {}  # field name -> CTkOptionMenu
        self.plans = []
        self.report_rows = []
        self.tree_item_to_plan = {}  # tree iid -> Plan
        self.queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None

        # ----- Product Images tab state -----
        self.img_client = None
        self.products = []
        self.image_plans = []
        self.image_report_rows = []
        self.image_folder = None
        self.img_tree_item_to_plan = {}  # tree iid -> ImagePlan
        self.img_queue = queue.Queue()
        self.img_cancel_event = threading.Event()
        self.img_worker = None

        # ----- Manage Images tab state -----
        self.mgmt_product = None
        self.attached_images = []
        self.mgmt_report_rows = []
        self.mgmt_tree_item_to_img = {}  # tree iid -> AttachedImage
        self.mgmt_queue = queue.Queue()
        self.mgmt_cancel_event = threading.Event()
        self.mgmt_worker = None

        # ----- Reporting tab state -----
        self.rpt_client = None
        self.rpt_store = None
        self.extract_plans = []
        self.rpt_report_rows = []
        self.rpt_entity_vars = {}        # entity -> BooleanVar
        self.rpt_tree_item_to_plan = {}  # tree iid -> ExtractPlan
        self.rpt_queue = queue.Queue()
        self.rpt_cancel_event = threading.Event()
        self.rpt_worker = None

        # ----- Dashboards tab state -----
        self.dash_last_path = None
        self.dash_queue = queue.Queue()
        self.dash_cancel_event = threading.Event()
        self.dash_worker = None

        self._build_ui()
        self._load_saved_settings()

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._style_treeview()

        self.tabview = ctk.CTkTabview(
            self, fg_color=BG, segmented_button_fg_color=CARD,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
            segmented_button_unselected_color=CARD,
            segmented_button_unselected_hover_color=BORDER,
            text_color=TEXT, corner_radius=10)
        self.tabview.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self._build_geo_tab(self.tabview.add("Customer Geolocation"))
        self._build_image_tab(self.tabview.add("Product Images"))
        self._build_manage_tab(self.tabview.add("Manage Images"))
        self._build_reporting_tab(self.tabview.add("Reporting"))
        self._build_dashboard_tab(self.tabview.add("Dashboards"))

    def _build_geo_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)

        # ----- Header -----
        header = ctk.CTkFrame(parent, fg_color="transparent")
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
        top = ctk.CTkFrame(parent, fg_color="transparent")
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

        self.country_entry = self._labeled_entry(
            conn, "Country (2-letter, optional)", 3)

        self.remember_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(conn, text="Remember settings (never API keys)",
                        variable=self.remember_var,
                        checkbox_width=20, checkbox_height=20,
                        corner_radius=5, border_color=BORDER,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        text_color=TEXT).grid(
            row=4, column=0, columnspan=2, padx=14, pady=6, sticky="w")

        self.connect_btn = self._button(
            conn, "Connect & Load Customers", self.on_connect)
        self.connect_btn.grid(row=5, column=0, columnspan=2,
                              padx=14, pady=(6, 14), sticky="ew")

        # Mapping panel
        mapping = self._card(top)
        mapping.grid(row=0, column=1, padx=(8, 0), pady=0, sticky="nsew")
        mapping.grid_rowconfigure(1, weight=1)
        mapping.grid_columnconfigure(0, weight=1)
        self._section_title(mapping, "2", "Map address field(s)").grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")

        # Fixed-height container so the scrollable frame's large natural size
        # doesn't force the whole card tall (it scrolls internally instead).
        fields_container = ctk.CTkFrame(mapping, fg_color="transparent",
                                        height=104)
        fields_container.grid(row=1, column=0, padx=14, pady=4, sticky="nsew")
        fields_container.grid_propagate(False)
        fields_container.grid_rowconfigure(0, weight=1)
        fields_container.grid_columnconfigure(0, weight=1)
        self.fields_frame = ctk.CTkScrollableFrame(
            fields_container, fg_color=FIELD, corner_radius=10)
        self.fields_frame.grid(row=0, column=0, sticky="nsew")
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
        actions = self._card(parent)
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
        table_frame = self._card(parent)
        table_frame.grid(row=3, column=0, padx=16, pady=6, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        columns = ("include", "name", "address", "lat", "lng",
                   "precision", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns,
                                 show="headings", style="Dark.Treeview",
                                 height=6)
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
        bottom = self._card(parent)
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
            bottom, height=90, fg_color="#151515", text_color="#b8b8b8",
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
            self.img_instance_entry.insert(0, cfg["instance_name"])
        if cfg.get("country"):
            self.country_entry.insert(0, cfg["country"])
        self.replace_var.set(bool(cfg.get("replace_existing", False)))
        self.img_replace_var.set(bool(cfg.get("image_replace_existing", False)))
        self.img_move_var.set(bool(cfg.get("image_move_processed", False)))
        self._saved_fields = cfg.get("address_fields", [])
        self._saved_roles = cfg.get("field_roles", {}) or {}
        # Reporting tab: period + entity selection (never credentials).
        saved_period = cfg.get("reporting_period")
        if saved_period in PERIOD_LABEL_BY_KEY:
            self.rpt_period_var.set(PERIOD_LABEL_BY_KEY[saved_period])
        saved_entities = cfg.get("reporting_entities")
        if saved_entities:
            for name, var in self.rpt_entity_vars.items():
                var.set(name in saved_entities)
        # API keys are never remembered; purge any an older version stored.
        settings.purge_saved_credentials(cfg.get("instance_name"))

    def _persist_settings(self):
        if not self.remember_var.get():
            return
        instance = self.instance_entry.get().strip()
        field_roles = self._field_roles()
        cfg = settings.load_config()  # merge, so image-tab keys aren't clobbered
        cfg.update({
            "instance_name": instance,
            "country": self.country_entry.get().strip().upper(),
            "replace_existing": self.replace_var.get(),
            "address_fields": [name for name, _role in field_roles],
            "field_roles": {name: role for name, role in field_roles},
        })
        settings.save_config(cfg)
        # API keys are deliberately not persisted.

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
        country = self.country_entry.get().strip().upper()
        if not (instance and skynamo_key):
            self.set_status("Enter instance name and Skynamo key first.")
            return
        if country and len(country) != 2:
            self.set_status("Country must be a 2-letter code (e.g. ZA) or blank.")
            return
        self.country = country or None
        self.log_line(f"Connecting to '{instance}'...")

        def work():
            try:
                client = SkynamoClient(instance, skynamo_key)
                ok, message = client.test_connection()
                if not ok:
                    self.queue.put(("error", message))
                    return
                self.queue.put(("log", "Skynamo credentials OK."))
                geocoder = NominatimGeocoder()
                self.queue.put(("status", "Validating OpenStreetMap geocoder..."))
                geocoder.validate(country=self.country)
                self.queue.put(("log", "OpenStreetMap geocoder OK."))
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
        self.field_role_vars = {}
        self.field_role_menus = {}
        names = collect_custom_field_names(self.customers)
        if not names:
            ctk.CTkLabel(self.fields_frame,
                         text="No custom fields found on customers.",
                         text_color=TEXT_MUTED).pack(anchor="w", padx=8,
                                                     pady=8)
            return
        saved = set(getattr(self, "_saved_fields", []) or [])
        saved_roles = getattr(self, "_saved_roles", {}) or {}
        self.fields_frame.grid_columnconfigure(0, weight=1)
        for row, name in enumerate(names):
            var = ctk.BooleanVar(value=name in saved)
            var.trace_add("write", lambda *_: self._update_sample())
            ctk.CTkCheckBox(self.fields_frame, text=name, variable=var,
                            checkbox_width=18, checkbox_height=18,
                            corner_radius=4, border_color=BORDER,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            text_color=TEXT).grid(
                row=row, column=0, sticky="w", padx=(8, 6), pady=3)

            role_key = saved_roles.get(name, DEFAULT_ROLE)
            role_var = ctk.StringVar(
                value=ROLE_LABEL_BY_KEY.get(role_key,
                                            ROLE_LABEL_BY_KEY[DEFAULT_ROLE]))
            role_var.trace_add("write", lambda *_: self._update_sample())
            menu = ctk.CTkOptionMenu(
                self.fields_frame, values=ROLE_LABELS, variable=role_var,
                width=150, height=26, corner_radius=8, fg_color=FIELD,
                button_color=BORDER, button_hover_color=ACCENT,
                text_color=TEXT, dropdown_fg_color=CARD,
                dropdown_text_color=TEXT, dropdown_hover_color=BORDER)
            menu.grid(row=row, column=1, sticky="e", padx=(6, 8), pady=3)
            self.field_vars[name] = var
            self.field_role_vars[name] = role_var
            self.field_role_menus[name] = menu
        self._update_sample()

    def _field_roles(self):
        """Ordered [(field_name, role_key)] for the ticked fields."""
        result = []
        for name, var in self.field_vars.items():
            if var.get():
                label = self.field_role_vars[name].get()
                result.append((name, ROLE_BY_LABEL.get(label, DEFAULT_ROLE)))
        return result

    def _update_sample(self):
        field_roles = self._field_roles()
        if not field_roles:
            self.sample_label.configure(text="Sample address: -")
            return
        sample = next((build_query(c, field_roles).text for c in self.customers
                       if build_query(c, field_roles).text), None)
        self.sample_label.configure(
            text=f"Sample address: {sample or '(no customer has these fields filled)'}")

    # -- Step 2: preview (geocode only) ----------------------------------

    def on_preview(self):
        field_roles = self._field_roles()
        if not field_roles:
            self.set_status("Select at least one address field.")
            return
        replace = self.replace_var.get()
        country = self.country
        self.plans = []
        self.report_rows = []
        self.save_btn.configure(state="disabled")
        self._clear_tree()
        labels = [f"{name} ({ROLE_LABEL_BY_KEY[role]})"
                  for name, role in field_roles]
        self.log_line("Geocoding with fields: " + ", ".join(labels))
        self._persist_settings()

        def work():
            try:
                plans = engine.geocode_customers(
                    self.geocoder, self.customers, field_roles,
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

    # ====================================================================
    # Product Images tab
    # ====================================================================

    def _build_image_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)

        # ----- Header -----
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(14, 4), sticky="ew")
        ctk.CTkLabel(header, text="Product Image Import",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(header,
                     text="match images to products by code · preview · upload",
                     font=ctk.CTkFont(size=12),
                     text_color=TEXT_MUTED).pack(side="left", padx=(12, 0),
                                                 pady=(4, 0))

        # ----- Top: connection + folder side by side -----
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=1, column=0, padx=16, pady=(6, 6), sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        conn = self._card(top)
        conn.grid(row=0, column=0, padx=(0, 8), pady=0, sticky="nsew")
        self._section_title(conn, "1", "Connection").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(12, 6), sticky="w")
        self.img_instance_entry = self._labeled_entry(conn, "Instance name", 1)
        self.img_skynamo_entry = self._labeled_entry(
            conn, "Skynamo API key", 2, show="*")
        self.img_connect_btn = self._button(
            conn, "Connect & Load Products", self.on_img_connect)
        self.img_connect_btn.grid(row=3, column=0, columnspan=2,
                                  padx=14, pady=(6, 14), sticky="ew")

        folder = self._card(top)
        folder.grid(row=0, column=1, padx=(8, 0), pady=0, sticky="nsew")
        folder.grid_columnconfigure(0, weight=1)
        self._section_title(folder, "2", "Image folder").grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")
        self.img_folder_label = ctk.CTkLabel(
            folder, text="No folder selected.", anchor="w",
            wraplength=440, justify="left", text_color=TEXT_MUTED)
        self.img_folder_label.grid(row=1, column=0, padx=14, pady=4, sticky="ew")
        self.img_choose_btn = self._button(
            folder, "Choose folder…", self.on_choose_folder,
            fg_color=FIELD, hover_color=BORDER)
        self.img_choose_btn.grid(row=2, column=0, padx=14, pady=6, sticky="ew")
        ctk.CTkLabel(folder,
                     text="Files are named by product code (PNG/JPG). "
                          "Multiple images: CODE_1, CODE 2, CODE_A. A '/' in "
                          "a code becomes '-' in the filename.",
                     anchor="w", wraplength=440, justify="left",
                     text_color=TEXT_MUTED).grid(
            row=3, column=0, padx=14, pady=(0, 6), sticky="ew")

        self.img_replace_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            folder,
            text="Replace existing images (removes anything already on the product)",
            variable=self.img_replace_var, checkbox_width=20, checkbox_height=20,
            corner_radius=5, border_color=BORDER, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color=TEXT).grid(
            row=4, column=0, padx=14, pady=(0, 4), sticky="w")

        self.img_move_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            folder,
            text=f"After uploading, move processed files into "
                 f"'{IMAGE_FOLDER_SUCCESS}' and '{IMAGE_FOLDER_FAILED}' subfolders",
            variable=self.img_move_var, checkbox_width=20, checkbox_height=20,
            corner_radius=5, border_color=BORDER, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color=TEXT).grid(
            row=5, column=0, padx=14, pady=(0, 14), sticky="w")

        # ----- Action bar -----
        actions = self._card(parent)
        actions.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        self.img_preview_btn = self._button(
            actions, "Preview (match only)", self.on_img_preview,
            state="disabled")
        self.img_preview_btn.pack(side="left", padx=(12, 6), pady=10)
        self.img_upload_btn = self._button(
            actions, "Upload Selected to Skynamo", self.on_img_upload,
            state="disabled", fg_color=GREEN, hover_color=GREEN_HOVER)
        self.img_upload_btn.pack(side="left", padx=6, pady=10)
        self.img_cancel_btn = self._button(
            actions, "Cancel", self.on_img_cancel, state="disabled",
            fg_color=RED, hover_color=RED_HOVER, width=90)
        self.img_cancel_btn.pack(side="left", padx=6, pady=10)
        self.img_save_btn = self._button(
            actions, "Save Report CSV", self.on_img_save_report,
            state="disabled", fg_color=FIELD, hover_color=BORDER)
        self.img_save_btn.pack(side="left", padx=6, pady=10)
        self.img_select_all_btn = self._button(
            actions, "Select all", lambda: self._img_set_all_includes(True),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.img_select_all_btn.pack(side="right", padx=(6, 12), pady=10)
        self.img_select_none_btn = self._button(
            actions, "Select none", lambda: self._img_set_all_includes(False),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.img_select_none_btn.pack(side="right", padx=6, pady=10)

        # ----- Results table -----
        table_frame = self._card(parent)
        table_frame.grid(row=3, column=0, padx=16, pady=6, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        columns = ("include", "file", "code", "product", "seq", "existing",
                   "status")
        self.img_tree = ttk.Treeview(table_frame, columns=columns,
                                     show="headings", style="Dark.Treeview",
                                     height=6)
        headings = {
            "include": ("Use", 45), "file": ("File", 200),
            "code": ("Product code", 120), "product": ("Matched product", 180),
            "seq": ("Seq", 50), "existing": ("Existing", 70),
            "status": ("Status", 150),
        }
        for col, (text, width) in headings.items():
            self.img_tree.heading(col, text=text)
            anchor = "center" if col in ("include", "seq", "existing") else "w"
            self.img_tree.column(col, width=width, anchor=anchor)
        self.img_tree.tag_configure("low", background="#3a2f12",
                                    foreground="#f0c453")
        self.img_tree.tag_configure("skip", foreground="#7a7a7a")
        self.img_tree.tag_configure("fail", background="#3a1717",
                                    foreground="#f08c8c")
        self.img_tree.grid(row=0, column=0, padx=(10, 0), pady=10, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical",
                                command=self.img_tree.yview,
                                style="Dark.Vertical.TScrollbar")
        self.img_tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="ns")
        self.img_tree.bind("<Button-1>", self._img_on_tree_click)

        # ----- Bottom: progress + status + log -----
        bottom = self._card(parent)
        bottom.grid(row=4, column=0, padx=16, pady=(6, 14), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self.img_progress = ctk.CTkProgressBar(
            bottom, progress_color=ACCENT, fg_color=FIELD, height=8,
            corner_radius=4)
        self.img_progress.set(0)
        self.img_progress.grid(row=0, column=0, padx=14, pady=(12, 4),
                               sticky="ew")
        self.img_status_label = ctk.CTkLabel(bottom, text="Ready.", anchor="w",
                                             text_color=TEXT)
        self.img_status_label.grid(row=1, column=0, padx=14, pady=2, sticky="ew")
        self.img_log = ctk.CTkTextbox(
            bottom, height=90, fg_color="#151515", text_color="#b8b8b8",
            corner_radius=10, border_width=1, border_color=BORDER,
            font=ctk.CTkFont(family="Consolas", size=12))
        self.img_log.grid(row=2, column=0, padx=14, pady=(4, 14), sticky="ew")

    # -- Image logging / status -------------------------------------------

    def img_log_line(self, text):
        self.img_log.insert("end", text + "\n")
        self.img_log.see("end")

    def img_set_status(self, text):
        self.img_status_label.configure(text=text)

    def _persist_image_settings(self):
        if not self.remember_var.get():
            return
        cfg = settings.load_config()
        cfg["instance_name"] = self.img_instance_entry.get().strip()
        cfg["image_replace_existing"] = self.img_replace_var.get()
        cfg["image_move_processed"] = self.img_move_var.get()
        settings.save_config(cfg)  # merge; never persists API keys

    # -- Image worker plumbing --------------------------------------------

    def _img_start_worker(self, target):
        self.img_cancel_event.clear()
        self._img_set_busy(True)
        self.img_worker = threading.Thread(target=target, daemon=True)
        self.img_worker.start()
        self.after(100, self._img_poll_queue)

    def _img_poll_queue(self):
        try:
            while True:
                kind, payload = self.img_queue.get_nowait()
                if kind == "progress":
                    self._img_on_progress(payload)
                elif kind == "log":
                    self.img_log_line(payload)
                elif kind == "status":
                    self.img_set_status(payload)
                elif kind == "done":
                    payload()
                    self._img_set_busy(False)
                    return
                elif kind == "error":
                    self.img_log_line(f"ERROR: {payload}")
                    self.img_set_status("Error - see log.")
                    self._img_set_busy(False)
                    return
        except queue.Empty:
            pass
        self.after(100, self._img_poll_queue)

    def _img_on_progress(self, ev):
        if ev["total"]:
            self.img_progress.set(ev["index"] / ev["total"])
        self.img_set_status(f"{ev['phase'].title()} {ev['index']}/{ev['total']}: "
                            f"{ev['name']}")

    def _img_set_busy(self, busy):
        self.img_cancel_btn.configure(state="normal" if busy else "disabled")
        for btn in (self.img_connect_btn, self.img_choose_btn,
                    self.img_preview_btn, self.img_upload_btn,
                    self.img_save_btn, self.img_select_all_btn,
                    self.img_select_none_btn):
            btn.configure(state="disabled" if busy else btn.cget("state"))
        if not busy:
            self.img_connect_btn.configure(state="normal")
            self.img_choose_btn.configure(state="normal")
            if self.img_client is not None and self.image_folder:
                self.img_preview_btn.configure(state="normal")
            if self.image_plans:
                self.img_upload_btn.configure(state="normal")
                self.img_select_all_btn.configure(state="normal")
                self.img_select_none_btn.configure(state="normal")
            if self.image_report_rows:
                self.img_save_btn.configure(state="normal")

    # -- Step 1: connect & load products ----------------------------------

    def on_img_connect(self):
        instance = self.img_instance_entry.get().strip()
        skynamo_key = self.img_skynamo_entry.get().strip()
        if not (instance and skynamo_key):
            self.img_set_status("Enter instance name and Skynamo key first.")
            return
        self.img_log_line(f"Connecting to '{instance}'...")

        def work():
            try:
                client = SkynamoClient(instance, skynamo_key)
                ok, message = client.test_connection()
                if not ok:
                    self.img_queue.put(("error", message))
                    return
                self.img_queue.put(("log", "Skynamo credentials OK."))
                self.img_queue.put(("status", "Fetching products..."))
                products = client.fetch_all_products(
                    on_page=lambda n, total: self.img_queue.put((
                        "status", f"Fetched {n}"
                        f"{f' of {total}' if total else ''} products...")))

                def finish():
                    self.img_client = client
                    self.products = products
                    self.img_log_line(f"Loaded {len(products)} products.")
                    self.img_set_status(
                        f"Loaded {len(products)} products. "
                        f"Choose a folder, then Preview.")
                    self._persist_image_settings()
                self.img_queue.put(("done", finish))
            except Exception as exc:
                self.img_queue.put(("error", str(exc)))

        self._img_start_worker(work)

    # -- Step 2: choose folder + preview (match only) ---------------------

    def on_choose_folder(self):
        folder = filedialog.askdirectory(title="Select the image folder")
        if not folder:
            return
        self.image_folder = folder
        self.img_folder_label.configure(text=folder, text_color=TEXT)
        self.img_log_line(f"Image folder: {folder}")
        if self.img_client is not None:
            self.img_preview_btn.configure(state="normal")
        self.img_set_status("Folder selected. Preview to match images.")

    def on_img_preview(self):
        if self.img_client is None:
            self.img_set_status("Connect and load products first.")
            return
        if not self.image_folder:
            self.img_set_status("Choose an image folder first.")
            return
        self.image_plans = []
        self.image_report_rows = []
        self.img_save_btn.configure(state="disabled")
        self._img_clear_tree()
        self.img_log_line("Matching images in: " + self.image_folder)

        def work():
            try:
                plans = image_engine.scan_images(
                    self.products, self.image_folder,
                    on_progress=lambda ev: self.img_queue.put(("progress", ev)),
                    should_cancel=self.img_cancel_event.is_set)

                def finish():
                    self.image_plans = plans
                    self._img_populate_tree(plans)
                    counts = image_engine.summarize(plans)
                    self.img_set_status(
                        self._img_summary_text(counts, preview=True))
                    self.img_log_line("Preview complete. Review rows, then "
                                      "'Upload Selected to Skynamo'.")
                self.img_queue.put(("done", finish))
            except Exception as exc:
                self.img_queue.put(("error", str(exc)))

        self._img_start_worker(work)

    # -- Step 3: upload to Skynamo ----------------------------------------

    def on_img_upload(self):
        to_upload = [p for p in self.image_plans if p.include and p.writable]
        if not to_upload:
            self.img_set_status("No images selected to upload.")
            return
        replace = self.img_replace_var.get()
        move_processed = self.img_move_var.get()
        self._persist_image_settings()
        self.img_log_line(
            f"Uploading {len(to_upload)} images to Skynamo"
            f"{' (replacing existing)' if replace else ''}...")
        if move_processed:
            self.img_log_line(
                f"Processed files will be moved into '{IMAGE_FOLDER_SUCCESS}' "
                f"and '{IMAGE_FOLDER_FAILED}' under the image folder.")

        def work():
            try:
                report_rows = image_engine.upload_images(
                    self.img_client, self.image_plans,
                    replace_existing=replace, move_processed=move_processed,
                    on_progress=lambda ev: self.img_queue.put(("progress", ev)),
                    should_cancel=self.img_cancel_event.is_set)

                def finish():
                    self.image_report_rows = report_rows
                    self._img_refresh_tree_statuses()
                    counts = image_engine.summarize(self.image_plans)
                    self.img_set_status(
                        self._img_summary_text(counts, preview=False))
                    self._img_log_filing(move_processed)
                    self.img_log_line("Upload complete. Save the report if needed.")
                    self.img_save_btn.configure(state="normal")
                self.img_queue.put(("done", finish))
            except Exception as exc:
                self.img_queue.put(("error", str(exc)))

        self._img_start_worker(work)

    def on_img_cancel(self):
        self.img_cancel_event.set()
        self.img_set_status("Cancelling...")
        self.img_log_line("Cancel requested - stopping after current item.")

    # -- Step 4: save report ----------------------------------------------

    def on_img_save_report(self):
        rows = (self.image_report_rows
                or [p.to_report_row() for p in self.image_plans])
        if not rows:
            self.img_set_status("Nothing to save yet.")
            return
        default = (f"product_images_report_"
                   f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        image_engine.write_report(rows, path)
        self.img_log_line(f"Report saved to: {path}")
        self.img_set_status(f"Report saved to {path}")

    def _img_log_filing(self, requested):
        """Log where the processed files went (and any that could not move)."""
        if not requested:
            return
        filed = image_engine.filing_summary(self.image_plans)
        if filed:
            self.img_log_line("Filed: " + ", ".join(
                f"{n} into '{name}'" for name, n in sorted(filed.items())))
        stuck = image_engine.filing_failures(self.image_plans)
        for plan in stuck:
            self.img_log_line(f"  NOT moved - {plan.filename}: {plan.notes}")
        if not filed and not stuck:
            self.img_log_line(
                "Nothing to file - the run was cancelled, or no image "
                "reached an outcome.")

    # -- Image tree helpers -----------------------------------------------

    def _img_clear_tree(self):
        self.img_tree.delete(*self.img_tree.get_children())
        self.img_tree_item_to_plan = {}

    def _img_row_tag(self, plan):
        if plan.status in (STATUS_IMG_NO_MATCH, STATUS_IMG_BAD_FORMAT):
            return "skip"
        if plan.status == STATUS_IMG_UPLOAD_FAILED:
            return "fail"
        if plan.status == STATUS_IMG_AMBIGUOUS:
            return "low"
        return ""

    def _img_plan_values(self, plan):
        check = CHECK_ON if (plan.include and plan.writable) else CHECK_OFF
        if not plan.writable:
            check = ""
        existing = (len(plan.product.get("files") or [])
                    if plan.product else "")
        return (check, plan.filename, plan.product_code, plan.product_name,
                plan.sequence or "", existing, plan.status)

    def _img_populate_tree(self, plans):
        self._img_clear_tree()
        for plan in plans:
            tag = self._img_row_tag(plan)
            iid = self.img_tree.insert("", "end",
                                       values=self._img_plan_values(plan),
                                       tags=(tag,) if tag else ())
            self.img_tree_item_to_plan[iid] = plan

    def _img_refresh_tree_statuses(self):
        for iid, plan in self.img_tree_item_to_plan.items():
            tag = self._img_row_tag(plan)
            self.img_tree.item(iid, values=self._img_plan_values(plan),
                               tags=(tag,) if tag else ())

    def _img_on_tree_click(self, event):
        if self.img_tree.identify("region", event.x, event.y) != "cell":
            return
        if self.img_tree.identify_column(event.x) != "#1":  # only the Use column
            return
        iid = self.img_tree.identify_row(event.y)
        plan = self.img_tree_item_to_plan.get(iid)
        if not plan or not plan.writable:
            return
        plan.include = not plan.include
        self.img_tree.set(iid, "include",
                          CHECK_ON if plan.include else CHECK_OFF)

    def _img_set_all_includes(self, value):
        for iid, plan in self.img_tree_item_to_plan.items():
            if plan.writable:
                plan.include = value
                self.img_tree.set(iid, "include",
                                  CHECK_ON if value else CHECK_OFF)

    def _img_summary_text(self, counts, preview):
        matched = counts.get(STATUS_IMG_PENDING, 0)
        if not preview:
            matched += counts.get(STATUS_IMG_UPLOADED, 0)
        parts = [
            f"matched={matched}",
            f"uploaded={counts.get(STATUS_IMG_UPLOADED, 0)}",
            f"no-match={counts.get(STATUS_IMG_NO_MATCH, 0)}",
            f"ambiguous={counts.get(STATUS_IMG_AMBIGUOUS, 0)}",
            f"bad-format={counts.get(STATUS_IMG_BAD_FORMAT, 0)}",
        ]
        if not preview:
            parts.append(f"upload-fail={counts.get(STATUS_IMG_UPLOAD_FAILED, 0)}")
        label = "Preview" if preview else "Done"
        return f"{label}:  " + "   ".join(parts)

    # ====================================================================
    # Manage Images tab (view / remove images already on a product)
    # ====================================================================

    def _build_manage_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        # ----- Header -----
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(14, 4), sticky="ew")
        ctk.CTkLabel(header, text="Manage Product Images",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(header,
                     text="view a product's images · remove (detach) them",
                     font=ctk.CTkFont(size=12),
                     text_color=TEXT_MUTED).pack(side="left", padx=(12, 0),
                                                 pady=(4, 0))

        # ----- Lookup + actions -----
        top = self._card(parent)
        top.grid(row=1, column=0, padx=16, pady=(6, 6), sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        self._section_title(top, "1", "Find product").grid(
            row=0, column=0, columnspan=3, padx=14, pady=(12, 6), sticky="w")
        ctk.CTkLabel(top, text="Product code", anchor="w",
                     text_color=TEXT).grid(row=1, column=0, padx=(14, 6),
                                           pady=4, sticky="w")
        self.mgmt_code_entry = self._entry(top)
        self.mgmt_code_entry.grid(row=1, column=1, padx=(0, 6), pady=4,
                                  sticky="ew")
        self.mgmt_load_btn = self._button(top, "Load Images",
                                          self.on_mgmt_load, width=140)
        self.mgmt_load_btn.grid(row=1, column=2, padx=(0, 14), pady=4)
        ctk.CTkLabel(
            top,
            text="Connect & load products on the Product Images tab first. "
                 "Removing an image detaches it from this product (Skynamo has "
                 "no delete); the file itself may remain on the server.",
            anchor="w", wraplength=900, justify="left",
            text_color=TEXT_MUTED).grid(row=2, column=0, columnspan=3,
                                        padx=14, pady=(2, 6), sticky="ew")

        actions = ctk.CTkFrame(top, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=3, padx=10, pady=(0, 10),
                     sticky="ew")
        self.mgmt_delete_btn = self._button(
            actions, "Remove Selected from Product", self.on_mgmt_delete,
            state="disabled", fg_color=RED, hover_color=RED_HOVER)
        self.mgmt_delete_btn.pack(side="left", padx=(4, 6), pady=4)
        self.mgmt_cancel_btn = self._button(
            actions, "Cancel", self.on_mgmt_cancel, state="disabled",
            fg_color=FIELD, hover_color=BORDER, width=90)
        self.mgmt_cancel_btn.pack(side="left", padx=6, pady=4)
        self.mgmt_save_btn = self._button(
            actions, "Save Report CSV", self.on_mgmt_save_report,
            state="disabled", fg_color=FIELD, hover_color=BORDER)
        self.mgmt_save_btn.pack(side="left", padx=6, pady=4)
        self.mgmt_select_all_btn = self._button(
            actions, "Select all", lambda: self._mgmt_set_all_deletes(True),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.mgmt_select_all_btn.pack(side="right", padx=(6, 4), pady=4)
        self.mgmt_select_none_btn = self._button(
            actions, "Select none", lambda: self._mgmt_set_all_deletes(False),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.mgmt_select_none_btn.pack(side="right", padx=6, pady=4)

        # ----- Results table -----
        table_frame = self._card(parent)
        table_frame.grid(row=2, column=0, padx=16, pady=6, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        columns = ("remove", "filename", "guid", "status")
        self.mgmt_tree = ttk.Treeview(table_frame, columns=columns,
                                      show="headings", style="Dark.Treeview",
                                      height=6)
        headings = {
            "remove": ("Remove", 70), "filename": ("Filename", 280),
            "guid": ("File GUID", 320), "status": ("Status", 140),
        }
        for col, (text, width) in headings.items():
            self.mgmt_tree.heading(col, text=text)
            anchor = "center" if col == "remove" else "w"
            self.mgmt_tree.column(col, width=width, anchor=anchor)
        self.mgmt_tree.tag_configure("skip", foreground="#7a7a7a")
        self.mgmt_tree.tag_configure("fail", background="#3a1717",
                                     foreground="#f08c8c")
        self.mgmt_tree.tag_configure("gone", foreground="#6fae8f")
        self.mgmt_tree.grid(row=0, column=0, padx=(10, 0), pady=10,
                            sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical",
                                command=self.mgmt_tree.yview,
                                style="Dark.Vertical.TScrollbar")
        self.mgmt_tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="ns")
        self.mgmt_tree.bind("<Button-1>", self._mgmt_on_tree_click)

        # ----- Bottom: progress + status + log -----
        bottom = self._card(parent)
        bottom.grid(row=3, column=0, padx=16, pady=(6, 14), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self.mgmt_progress = ctk.CTkProgressBar(
            bottom, progress_color=ACCENT, fg_color=FIELD, height=8,
            corner_radius=4)
        self.mgmt_progress.set(0)
        self.mgmt_progress.grid(row=0, column=0, padx=14, pady=(12, 4),
                                sticky="ew")
        self.mgmt_status_label = ctk.CTkLabel(bottom, text="Ready.", anchor="w",
                                              text_color=TEXT)
        self.mgmt_status_label.grid(row=1, column=0, padx=14, pady=2,
                                    sticky="ew")
        self.mgmt_log = ctk.CTkTextbox(
            bottom, height=90, fg_color="#151515", text_color="#b8b8b8",
            corner_radius=10, border_width=1, border_color=BORDER,
            font=ctk.CTkFont(family="Consolas", size=12))
        self.mgmt_log.grid(row=2, column=0, padx=14, pady=(4, 14), sticky="ew")

    # -- Manage logging / status ------------------------------------------

    def mgmt_log_line(self, text):
        self.mgmt_log.insert("end", text + "\n")
        self.mgmt_log.see("end")

    def mgmt_set_status(self, text):
        self.mgmt_status_label.configure(text=text)

    # -- Manage worker plumbing -------------------------------------------

    def _mgmt_start_worker(self, target):
        self.mgmt_cancel_event.clear()
        self._mgmt_set_busy(True)
        self.mgmt_worker = threading.Thread(target=target, daemon=True)
        self.mgmt_worker.start()
        self.after(100, self._mgmt_poll_queue)

    def _mgmt_poll_queue(self):
        try:
            while True:
                kind, payload = self.mgmt_queue.get_nowait()
                if kind == "progress":
                    self._mgmt_on_progress(payload)
                elif kind == "log":
                    self.mgmt_log_line(payload)
                elif kind == "status":
                    self.mgmt_set_status(payload)
                elif kind == "done":
                    payload()
                    self._mgmt_set_busy(False)
                    return
                elif kind == "error":
                    self.mgmt_log_line(f"ERROR: {payload}")
                    self.mgmt_set_status("Error - see log.")
                    self._mgmt_set_busy(False)
                    return
        except queue.Empty:
            pass
        self.after(100, self._mgmt_poll_queue)

    def _mgmt_on_progress(self, ev):
        if ev["total"]:
            self.mgmt_progress.set(ev["index"] / ev["total"])
        self.mgmt_set_status(f"{ev['phase'].title()} {ev['index']}/{ev['total']}:"
                             f" {ev['name']}")

    def _mgmt_set_busy(self, busy):
        self.mgmt_cancel_btn.configure(state="normal" if busy else "disabled")
        for btn in (self.mgmt_load_btn, self.mgmt_delete_btn,
                    self.mgmt_save_btn, self.mgmt_select_all_btn,
                    self.mgmt_select_none_btn):
            btn.configure(state="disabled" if busy else btn.cget("state"))
        if not busy:
            self.mgmt_load_btn.configure(state="normal")
            if any(img.status == STATUS_ATT_LOADED
                   for img in self.attached_images):
                self.mgmt_delete_btn.configure(state="normal")
                self.mgmt_select_all_btn.configure(state="normal")
                self.mgmt_select_none_btn.configure(state="normal")
            if self.mgmt_report_rows:
                self.mgmt_save_btn.configure(state="normal")

    # -- Step 1: load a product's attached images -------------------------

    def on_mgmt_load(self):
        if self.img_client is None or not self.products:
            self.mgmt_set_status(
                "Connect & load products on the Product Images tab first.")
            return
        code = self.mgmt_code_entry.get().strip()
        if not code:
            self.mgmt_set_status("Enter a product code.")
            return
        product = next((p for p in self.products
                        if product_code_of(p).casefold() == code.casefold()),
                       None)
        if product is None:
            self.mgmt_set_status(f"No product with code '{code}'.")
            return
        self.mgmt_product = product
        self.attached_images = []
        self.mgmt_report_rows = []
        self.mgmt_save_btn.configure(state="disabled")
        self._mgmt_clear_tree()
        files = product.get("files") or []
        self.mgmt_log_line(
            f"Loading {len(files)} image(s) for product "
            f"'{product_code_of(product)}'...")
        if not files:
            self.mgmt_set_status("This product has no attached images.")
            return

        def work():
            try:
                images = image_engine.list_attached_images(
                    self.img_client, product,
                    on_progress=lambda ev: self.mgmt_queue.put(("progress", ev)),
                    should_cancel=self.mgmt_cancel_event.is_set)

                def finish():
                    self.attached_images = images
                    self._mgmt_populate_tree(images)
                    loaded = sum(1 for i in images
                                 if i.status == STATUS_ATT_LOADED)
                    failed = sum(1 for i in images
                                 if i.status == STATUS_ATT_FETCH_FAILED)
                    self.mgmt_set_status(
                        f"Loaded {loaded} image(s)"
                        f"{f', {failed} name(s) unresolved' if failed else ''}. "
                        f"Tick images to remove, then 'Remove Selected'.")
                self.mgmt_queue.put(("done", finish))
            except Exception as exc:
                self.mgmt_queue.put(("error", str(exc)))

        self._mgmt_start_worker(work)

    # -- Step 2: remove selected images -----------------------------------

    def on_mgmt_delete(self):
        to_delete = [i for i in self.attached_images if i.delete]
        if not to_delete:
            self.mgmt_set_status("No images ticked for removal.")
            return
        product = self.mgmt_product
        self.mgmt_log_line(
            f"Removing {len(to_delete)} image(s) from "
            f"'{product_code_of(product)}'...")

        def work():
            try:
                report_rows = image_engine.delete_selected_images(
                    self.img_client, product, self.attached_images,
                    on_progress=lambda ev: self.mgmt_queue.put(("progress", ev)),
                    should_cancel=self.mgmt_cancel_event.is_set)
                # Keep the in-memory product's files in sync with what remains,
                # so the Product Images tab's "Existing" count stays accurate.
                remaining = [i.guid for i in self.attached_images
                             if i.status != STATUS_ATT_DELETED]
                product["files"] = remaining

                def finish():
                    self.mgmt_report_rows = report_rows
                    self._mgmt_refresh_tree_statuses()
                    removed = sum(1 for i in self.attached_images
                                  if i.status == STATUS_ATT_DELETED)
                    self.mgmt_set_status(
                        f"Removed {removed} image(s). Save the report if needed.")
                    self.mgmt_log_line("Done.")
                    self.mgmt_save_btn.configure(state="normal")
                self.mgmt_queue.put(("done", finish))
            except Exception as exc:
                self.mgmt_queue.put(("error", str(exc)))

        self._mgmt_start_worker(work)

    def on_mgmt_cancel(self):
        self.mgmt_cancel_event.set()
        self.mgmt_set_status("Cancelling...")
        self.mgmt_log_line("Cancel requested - stopping after current item.")

    def on_mgmt_save_report(self):
        rows = (self.mgmt_report_rows
                or [i.to_report_row() for i in self.attached_images])
        if not rows:
            self.mgmt_set_status("Nothing to save yet.")
            return
        default = (f"product_images_removed_"
                   f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        image_engine.write_report(
            rows, path, fieldnames=ATTACHED_IMAGE_REPORT_FIELDNAMES)
        self.mgmt_log_line(f"Report saved to: {path}")
        self.mgmt_set_status(f"Report saved to {path}")

    # -- Manage tree helpers ----------------------------------------------

    def _mgmt_clear_tree(self):
        self.mgmt_tree.delete(*self.mgmt_tree.get_children())
        self.mgmt_tree_item_to_img = {}

    def _mgmt_row_tag(self, img):
        if img.status == STATUS_ATT_FETCH_FAILED:
            return "skip"
        if img.status == STATUS_ATT_DELETE_FAILED:
            return "fail"
        if img.status == STATUS_ATT_DELETED:
            return "gone"
        return ""

    def _mgmt_deletable(self, img):
        return img.status in (STATUS_ATT_LOADED, STATUS_ATT_DELETE_FAILED)

    def _mgmt_img_values(self, img):
        check = CHECK_ON if img.delete else CHECK_OFF
        if not self._mgmt_deletable(img):
            check = ""
        return (check, img.filename, img.guid, img.status)

    def _mgmt_populate_tree(self, images):
        self._mgmt_clear_tree()
        for img in images:
            tag = self._mgmt_row_tag(img)
            iid = self.mgmt_tree.insert("", "end",
                                        values=self._mgmt_img_values(img),
                                        tags=(tag,) if tag else ())
            self.mgmt_tree_item_to_img[iid] = img

    def _mgmt_refresh_tree_statuses(self):
        for iid, img in self.mgmt_tree_item_to_img.items():
            tag = self._mgmt_row_tag(img)
            self.mgmt_tree.item(iid, values=self._mgmt_img_values(img),
                                tags=(tag,) if tag else ())

    def _mgmt_on_tree_click(self, event):
        if self.mgmt_tree.identify("region", event.x, event.y) != "cell":
            return
        if self.mgmt_tree.identify_column(event.x) != "#1":  # Remove column
            return
        iid = self.mgmt_tree.identify_row(event.y)
        img = self.mgmt_tree_item_to_img.get(iid)
        if not img or not self._mgmt_deletable(img):
            return
        img.delete = not img.delete
        self.mgmt_tree.set(iid, "remove",
                           CHECK_ON if img.delete else CHECK_OFF)

    def _mgmt_set_all_deletes(self, value):
        for iid, img in self.mgmt_tree_item_to_img.items():
            if self._mgmt_deletable(img):
                img.delete = value
                self.mgmt_tree.set(iid, "remove",
                                   CHECK_ON if value else CHECK_OFF)

    # ====================================================================
    # Reporting tab
    # ====================================================================

    def _build_reporting_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)

        # ----- Header -----
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(14, 4), sticky="ew")
        ctk.CTkLabel(header, text="Reporting Extract",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(header,
                     text="Reporting API · plan · extract to local store",
                     font=ctk.CTkFont(size=12),
                     text_color=TEXT_MUTED).pack(side="left", padx=(12, 0),
                                                 pady=(4, 0))

        # ----- Top: connection + extract options -----
        top = ctk.CTkFrame(parent, fg_color="transparent")
        top.grid(row=1, column=0, padx=16, pady=(6, 6), sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        conn = self._card(top)
        conn.grid(row=0, column=0, padx=(0, 8), pady=0, sticky="nsew")
        self._section_title(conn, "1", "Reporting API credentials").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(12, 6), sticky="w")
        self.rpt_id_entry = self._labeled_entry(conn, "Client ID", 1, show="*")
        self.rpt_secret_entry = self._labeled_entry(
            conn, "Client Secret", 2, show="*")
        ctk.CTkLabel(conn,
                     text="Skynamo insights → Settings → Integration Tokens →\n"
                          "\"Add client credential\"  (not \"Add access token\").\n"
                          "This is a separate paid add-on from the API key.",
                     anchor="w", justify="left", wraplength=420,
                     text_color=TEXT_MUTED).grid(
            row=3, column=0, columnspan=2, padx=14, pady=(2, 4), sticky="ew")
        self.rpt_connect_btn = self._button(
            conn, "Connect to Reporting API", self.on_rpt_connect)
        self.rpt_connect_btn.grid(row=4, column=0, columnspan=2,
                                  padx=14, pady=(6, 14), sticky="ew")

        opts = self._card(top)
        opts.grid(row=0, column=1, padx=(8, 0), pady=0, sticky="nsew")
        opts.grid_columnconfigure(0, weight=1)
        self._section_title(opts, "2", "What to extract").grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w")

        period_row = ctk.CTkFrame(opts, fg_color="transparent")
        period_row.grid(row=1, column=0, padx=14, pady=4, sticky="ew")
        ctk.CTkLabel(period_row, text="Reporting period", anchor="w",
                     text_color=TEXT).pack(side="left")
        self.rpt_period_var = ctk.StringVar(
            value=PERIOD_LABEL_BY_KEY[DEFAULT_REPORTING_PERIOD])
        self.rpt_period_var.trace_add("write",
                                      lambda *_: self._rpt_update_hint())
        ctk.CTkOptionMenu(
            period_row, values=PERIOD_LABELS, variable=self.rpt_period_var,
            width=210, height=28, corner_radius=8, fg_color=FIELD,
            button_color=BORDER, button_hover_color=ACCENT, text_color=TEXT,
            dropdown_fg_color=CARD, dropdown_text_color=TEXT,
            dropdown_hover_color=BORDER).pack(side="right")

        entities_row = ctk.CTkFrame(opts, fg_color="transparent")
        entities_row.grid(row=2, column=0, padx=14, pady=4, sticky="ew")
        for entity in REPORTING_ENTITIES:
            var = ctk.BooleanVar(value=True)
            var.trace_add("write", lambda *_: self._rpt_update_hint())
            ctk.CTkCheckBox(entities_row, text=entity, variable=var,
                            checkbox_width=18, checkbox_height=18,
                            corner_radius=4, border_color=BORDER,
                            fg_color=ACCENT, hover_color=ACCENT_HOVER,
                            text_color=TEXT).pack(side="left", padx=(0, 10))
            self.rpt_entity_vars[entity] = var

        self.rpt_hint_label = ctk.CTkLabel(
            opts, text="", anchor="w", justify="left", wraplength=430,
            text_color=TEXT_MUTED)
        self.rpt_hint_label.grid(row=3, column=0, padx=14, pady=(4, 4),
                                 sticky="ew")
        self.rpt_store_label = ctk.CTkLabel(
            opts, text="", anchor="w", justify="left", wraplength=430,
            text_color=TEXT_MUTED)
        self.rpt_store_label.grid(row=4, column=0, padx=14, pady=(0, 14),
                                  sticky="ew")

        # ----- Action bar -----
        actions = self._card(parent)
        actions.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        self.rpt_plan_btn = self._button(
            actions, "Plan Extract (no calls)", self.on_rpt_plan,
            state="disabled")
        self.rpt_plan_btn.pack(side="left", padx=(12, 6), pady=10)
        self.rpt_run_btn = self._button(
            actions, "Run Extract", self.on_rpt_run, state="disabled",
            fg_color=GREEN, hover_color=GREEN_HOVER)
        self.rpt_run_btn.pack(side="left", padx=6, pady=10)
        self.rpt_cancel_btn = self._button(
            actions, "Cancel", self.on_rpt_cancel, state="disabled",
            fg_color=RED, hover_color=RED_HOVER, width=90)
        self.rpt_cancel_btn.pack(side="left", padx=6, pady=10)
        self.rpt_save_btn = self._button(
            actions, "Save Report CSV", self.on_rpt_save_report,
            state="disabled", fg_color=FIELD, hover_color=BORDER)
        self.rpt_save_btn.pack(side="left", padx=6, pady=10)
        self.rpt_select_all_btn = self._button(
            actions, "Select all", lambda: self._rpt_set_all_includes(True),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.rpt_select_all_btn.pack(side="right", padx=(6, 12), pady=10)
        self.rpt_select_none_btn = self._button(
            actions, "Select none", lambda: self._rpt_set_all_includes(False),
            state="disabled", width=96, fg_color=FIELD, hover_color=BORDER)
        self.rpt_select_none_btn.pack(side="right", padx=6, pady=10)

        # ----- Results table -----
        table_frame = self._card(parent)
        table_frame.grid(row=3, column=0, padx=16, pady=6, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        columns = ("include", "entity", "mode", "period", "rows", "status")
        self.rpt_tree = ttk.Treeview(table_frame, columns=columns,
                                     show="headings", style="Dark.Treeview",
                                     height=6)
        headings = {
            "include": ("Use", 45), "entity": ("Entity", 150),
            "mode": ("Mode", 90), "period": ("Period", 130),
            "rows": ("Rows", 70), "status": ("Status", 160),
        }
        for col, (text, width) in headings.items():
            self.rpt_tree.heading(col, text=text)
            anchor = "center" if col in ("include", "rows") else "w"
            self.rpt_tree.column(col, width=width, anchor=anchor)
        self.rpt_tree.tag_configure("low", background="#3a2f12",
                                    foreground="#f0c453")
        self.rpt_tree.tag_configure("skip", foreground="#7a7a7a")
        self.rpt_tree.tag_configure("fail", background="#3a1717",
                                    foreground="#f08c8c")
        self.rpt_tree.grid(row=0, column=0, padx=(10, 0), pady=10,
                           sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical",
                                command=self.rpt_tree.yview,
                                style="Dark.Vertical.TScrollbar")
        self.rpt_tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="ns")
        self.rpt_tree.bind("<Button-1>", self._rpt_on_tree_click)

        # ----- Bottom: progress + status + log -----
        bottom = self._card(parent)
        bottom.grid(row=4, column=0, padx=16, pady=(6, 14), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self.rpt_progress = ctk.CTkProgressBar(
            bottom, progress_color=ACCENT, fg_color=FIELD, height=8,
            corner_radius=4)
        self.rpt_progress.set(0)
        self.rpt_progress.grid(row=0, column=0, padx=14, pady=(12, 4),
                               sticky="ew")
        self.rpt_status_label = ctk.CTkLabel(bottom, text="Ready.", anchor="w",
                                             text_color=TEXT)
        self.rpt_status_label.grid(row=1, column=0, padx=14, pady=2,
                                   sticky="ew")
        self.rpt_log = ctk.CTkTextbox(
            bottom, height=90, fg_color="#151515", text_color="#b8b8b8",
            corner_radius=10, border_width=1, border_color=BORDER,
            font=ctk.CTkFont(family="Consolas", size=12))
        self.rpt_log.grid(row=2, column=0, padx=14, pady=(4, 14), sticky="ew")

        self._rpt_update_hint()
        self._rpt_refresh_store_label()

    # -- Reporting logging / status ---------------------------------------

    def rpt_log_line(self, text):
        self.rpt_log.insert("end", text + "\n")
        self.rpt_log.see("end")

    def rpt_set_status(self, text):
        self.rpt_status_label.configure(text=text)

    def _rpt_period(self):
        return PERIOD_BY_LABEL.get(self.rpt_period_var.get(),
                                   DEFAULT_REPORTING_PERIOD)

    def _rpt_selected_entities(self):
        return [name for name, var in self.rpt_entity_vars.items() if var.get()]

    def _rpt_update_hint(self):
        """Show the period's published query allowance vs what's selected."""
        period = self._rpt_period()
        selected = self._rpt_selected_entities()
        max_calls, window = report_engine.rate_limit_for(period)
        billed = [e for e in selected
                  if REPORTING_ENTITIES[e].get("has_period")]
        text = (f"'{period}' allows {max_calls} queries per {window}s. "
                f"{len(billed)} of {len(selected)} selected entities count "
                f"against it.")
        if len(billed) > max_calls:
            text += "  This selection will be throttled - it will still finish, just slower."
        self.rpt_hint_label.configure(text=text)

    def _rpt_refresh_store_label(self):
        """Show where the store is and what's in it.

        Deliberately does not create the database just to render a label - if
        no extract has run yet there is nothing to open.
        """
        path = default_store_path()
        if self.rpt_store is None and not os.path.exists(path):
            self.rpt_store_label.configure(
                text=f"Store: {path}\n(created on the first extract)")
            return
        try:
            store = self.rpt_store or ReportStore(path)
            counts = store.counts()
            runs = store.last_runs()
            if self.rpt_store is None:
                store.close()
        except Exception as exc:
            self.rpt_store_label.configure(text=f"Store unavailable: {exc}")
            return
        live = {t: n for t, n in counts.items() if n}
        summary = ", ".join(f"{t}={n}" for t, n in sorted(live.items())) or "empty"
        last = ""
        if runs:
            newest = max(runs.values(), key=lambda r: r["run_id"])
            if newest["started_at"]:
                last = f"  Last extract: {newest['started_at']}."
        self.rpt_store_label.configure(
            text=f"Store: {default_store_path()}\nRows: {summary}.{last}")

    def _persist_reporting_settings(self):
        if not self.remember_var.get():
            return
        cfg = settings.load_config()
        cfg["reporting_period"] = self._rpt_period()
        cfg["reporting_entities"] = self._rpt_selected_entities()
        settings.save_config(cfg)  # merge; credentials are never persisted

    # -- Reporting worker plumbing ----------------------------------------

    def _rpt_start_worker(self, target):
        self.rpt_cancel_event.clear()
        self._rpt_set_busy(True)
        self.rpt_worker = threading.Thread(target=target, daemon=True)
        self.rpt_worker.start()
        self.after(100, self._rpt_poll_queue)

    def _rpt_poll_queue(self):
        try:
            while True:
                kind, payload = self.rpt_queue.get_nowait()
                if kind == "progress":
                    self._rpt_on_progress(payload)
                elif kind == "log":
                    self.rpt_log_line(payload)
                elif kind == "status":
                    self.rpt_set_status(payload)
                elif kind == "done":
                    payload()
                    self._rpt_set_busy(False)
                    return
                elif kind == "error":
                    self.rpt_log_line(f"ERROR: {payload}")
                    self.rpt_set_status("Error - see log.")
                    self._rpt_set_busy(False)
                    return
        except queue.Empty:
            pass
        self.after(100, self._rpt_poll_queue)

    def _rpt_on_progress(self, ev):
        if ev["total"]:
            self.rpt_progress.set(ev["index"] / ev["total"])
        self.rpt_set_status(f"{ev['phase'].title()} {ev['index']}/{ev['total']}: "
                            f"{ev['name']}")

    def _rpt_set_busy(self, busy):
        self.rpt_cancel_btn.configure(state="normal" if busy else "disabled")
        for btn in (self.rpt_connect_btn, self.rpt_plan_btn, self.rpt_run_btn,
                    self.rpt_save_btn, self.rpt_select_all_btn,
                    self.rpt_select_none_btn):
            btn.configure(state="disabled" if busy else btn.cget("state"))
        if not busy:
            self.rpt_connect_btn.configure(state="normal")
            if self.rpt_client is not None:
                self.rpt_plan_btn.configure(state="normal")
            if self.extract_plans:
                self.rpt_run_btn.configure(state="normal")
                self.rpt_select_all_btn.configure(state="normal")
                self.rpt_select_none_btn.configure(state="normal")
            if self.rpt_report_rows:
                self.rpt_save_btn.configure(state="normal")

    # -- Step 1: connect --------------------------------------------------

    def on_rpt_connect(self):
        client_id = self.rpt_id_entry.get().strip()
        client_secret = self.rpt_secret_entry.get().strip()
        if not (client_id and client_secret):
            self.rpt_set_status("Enter the Client ID and Client Secret first.")
            return
        self.rpt_log_line("Requesting a token from the Reporting API...")

        def work():
            try:
                client = ReportingClient(client_id, client_secret)
                ok, message = client.test_connection()
                if not ok:
                    self.rpt_queue.put(("error", message))
                    return
                self.rpt_queue.put(("log", message))
                store = ReportStore(default_store_path())

                def finish():
                    self.rpt_client = client
                    self.rpt_store = store
                    self.rpt_set_status("Connected. Plan an extract.")
                    self._rpt_refresh_store_label()
                    self._persist_reporting_settings()
                self.rpt_queue.put(("done", finish))
            except Exception as exc:
                self.rpt_queue.put(("error", str(exc)))

        self._rpt_start_worker(work)

    # -- Step 2: plan (no network calls) ----------------------------------

    def on_rpt_plan(self):
        entities = self._rpt_selected_entities()
        if not entities:
            self.rpt_set_status("Select at least one entity.")
            return
        if self.rpt_store is None:
            self.rpt_set_status("Connect first.")
            return
        period = self._rpt_period()
        self.extract_plans = []
        self.rpt_report_rows = []
        self.rpt_save_btn.configure(state="disabled")
        self._rpt_clear_tree()
        self.rpt_log_line(f"Planning extract for '{period}': "
                          + ", ".join(entities))
        self._persist_reporting_settings()

        def work():
            try:
                plans = report_engine.plan_extract(
                    self.rpt_store, entities, period,
                    on_progress=lambda ev: self.rpt_queue.put(("progress", ev)),
                    should_cancel=self.rpt_cancel_event.is_set)

                def finish():
                    self.extract_plans = plans
                    self._rpt_populate_tree(plans)
                    counts = report_engine.summarize(plans)
                    self.rpt_set_status(
                        self._rpt_summary_text(counts, planned=True))
                    self.rpt_log_line(
                        "Plan complete (no API calls made). Review, then "
                        "'Run Extract'.")
                self.rpt_queue.put(("done", finish))
            except Exception as exc:
                self.rpt_queue.put(("error", str(exc)))

        self._rpt_start_worker(work)

    # -- Step 3: run the extract -----------------------------------------

    def on_rpt_run(self):
        to_run = [p for p in self.extract_plans if p.include and p.writable]
        if not to_run:
            self.rpt_set_status("No entities selected to extract.")
            return
        started_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.rpt_log_line(f"Extracting {len(to_run)} entity set(s)...")

        def work():
            try:
                report_rows = report_engine.run_extract(
                    self.rpt_client, self.rpt_store, self.extract_plans,
                    on_progress=lambda ev: self.rpt_queue.put(("progress", ev)),
                    should_cancel=self.rpt_cancel_event.is_set,
                    started_at=started_at)

                def finish():
                    self.rpt_report_rows = report_rows
                    self._rpt_refresh_tree_statuses()
                    counts = report_engine.summarize(self.extract_plans)
                    self.rpt_set_status(
                        self._rpt_summary_text(counts, planned=False))
                    for plan in self.extract_plans:
                        if plan.date_range:
                            self.rpt_log_line(
                                f"  {plan.entity}: server window "
                                f"{plan.date_range}")
                    self.rpt_log_line("Extract complete.")
                    self._rpt_refresh_store_label()
                    self.rpt_save_btn.configure(state="normal")
                self.rpt_queue.put(("done", finish))
            except Exception as exc:
                self.rpt_queue.put(("error", str(exc)))

        self._rpt_start_worker(work)

    def on_rpt_cancel(self):
        self.rpt_cancel_event.set()
        self.rpt_set_status("Cancelling...")
        self.rpt_log_line("Cancel requested - stopping after current entity.")

    # -- Step 4: save report ---------------------------------------------

    def on_rpt_save_report(self):
        rows = (self.rpt_report_rows
                or [p.to_report_row() for p in self.extract_plans])
        if not rows:
            self.rpt_set_status("Nothing to save yet.")
            return
        default = (f"reporting_extract_"
                   f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        report_engine.write_report(rows, path,
                                   fieldnames=REPORTING_REPORT_FIELDNAMES)
        self.rpt_log_line(f"Report saved to: {path}")
        self.rpt_set_status(f"Report saved to {path}")

    # -- Reporting tree helpers ------------------------------------------

    def _rpt_clear_tree(self):
        self.rpt_tree.delete(*self.rpt_tree.get_children())
        self.rpt_tree_item_to_plan = {}

    def _rpt_row_tag(self, plan):
        if plan.status == STATUS_RPT_SKIPPED:
            return "skip"
        if plan.status == STATUS_RPT_FAILED:
            return "fail"
        if "exceeds" in (plan.notes or ""):
            return "low"      # will be throttled - worth flagging
        return ""

    def _rpt_plan_values(self, plan):
        check = CHECK_ON if (plan.include and plan.writable) else CHECK_OFF
        if not plan.writable:
            check = ""
        return (check, plan.entity, plan.mode,
                plan.reporting_period or "(n/a)", plan.rows or "", plan.status)

    def _rpt_populate_tree(self, plans):
        self._rpt_clear_tree()
        for plan in plans:
            tag = self._rpt_row_tag(plan)
            iid = self.rpt_tree.insert("", "end",
                                       values=self._rpt_plan_values(plan),
                                       tags=(tag,) if tag else ())
            self.rpt_tree_item_to_plan[iid] = plan

    def _rpt_refresh_tree_statuses(self):
        for iid, plan in self.rpt_tree_item_to_plan.items():
            tag = self._rpt_row_tag(plan)
            self.rpt_tree.item(iid, values=self._rpt_plan_values(plan),
                               tags=(tag,) if tag else ())

    def _rpt_on_tree_click(self, event):
        if self.rpt_tree.identify("region", event.x, event.y) != "cell":
            return
        if self.rpt_tree.identify_column(event.x) != "#1":
            return
        iid = self.rpt_tree.identify_row(event.y)
        plan = self.rpt_tree_item_to_plan.get(iid)
        if not plan or not plan.writable:
            return
        plan.include = not plan.include
        self.rpt_tree.set(iid, "include",
                          CHECK_ON if plan.include else CHECK_OFF)

    def _rpt_set_all_includes(self, value):
        for iid, plan in self.rpt_tree_item_to_plan.items():
            if plan.writable:
                plan.include = value
                self.rpt_tree.set(iid, "include",
                                  CHECK_ON if value else CHECK_OFF)

    def _rpt_summary_text(self, counts, planned):
        parts = [f"planned={counts.get(STATUS_RPT_PENDING, 0)}"]
        if not planned:
            parts = [f"extracted={counts.get(STATUS_RPT_EXTRACTED, 0)}",
                     f"pending={counts.get(STATUS_RPT_PENDING, 0)}"]
        parts.append(f"skipped={counts.get(STATUS_RPT_SKIPPED, 0)}")
        parts.append(f"failed={counts.get(STATUS_RPT_FAILED, 0)}")
        if not planned:
            total_rows = sum(p.rows for p in self.extract_plans)
            parts.append(f"rows={total_rows}")
        label = "Planned" if planned else "Done"
        return f"{label}:  " + "   ".join(parts)

    # ====================================================================
    # Dashboards tab
    # ====================================================================

    def _build_dashboard_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(14, 4), sticky="ew")
        ctk.CTkLabel(header, text="Dashboards",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color=TEXT).pack(side="left")
        ctk.CTkLabel(header,
                     text="build a shareable HTML dashboard from the local store",
                     font=ctk.CTkFont(size=12),
                     text_color=TEXT_MUTED).pack(side="left", padx=(12, 0),
                                                 pady=(4, 0))

        card = self._card(parent)
        card.grid(row=1, column=0, padx=16, pady=(6, 6), sticky="ew")
        card.grid_columnconfigure(1, weight=1)
        self._section_title(card, "1", "Dashboard options").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(12, 6), sticky="w")
        self.dash_title_entry = self._labeled_entry(card, "Title", 1)
        self.dash_title_entry.insert(0, "Skynamo Dashboard")
        self.dash_label_entry = self._labeled_entry(
            card, "Period label (optional)", 2)
        ctk.CTkLabel(card,
                     text="Reads only the local store - no API calls, so this "
                          "is free to rebuild as often as you like. The result "
                          "is one self-contained .html file you can email.",
                     anchor="w", justify="left", wraplength=760,
                     text_color=TEXT_MUTED).grid(
            row=3, column=0, columnspan=2, padx=14, pady=(2, 14), sticky="ew")

        actions = self._card(parent)
        actions.grid(row=2, column=0, padx=16, pady=6, sticky="ew")
        self.dash_build_btn = self._button(
            actions, "Build Dashboard", self.on_dash_build,
            fg_color=GREEN, hover_color=GREEN_HOVER)
        self.dash_build_btn.pack(side="left", padx=(12, 6), pady=10)
        self.dash_open_btn = self._button(
            actions, "Open in Browser", self.on_dash_open, state="disabled",
            fg_color=FIELD, hover_color=BORDER)
        self.dash_open_btn.pack(side="left", padx=6, pady=10)

        info = self._card(parent)
        info.grid(row=3, column=0, padx=16, pady=6, sticky="nsew")
        info.grid_columnconfigure(0, weight=1)
        info.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(info, text="Store contents", anchor="w",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=TEXT).grid(row=0, column=0, padx=14,
                                           pady=(12, 4), sticky="w")
        self.dash_info = ctk.CTkTextbox(
            info, fg_color="#151515", text_color="#b8b8b8", corner_radius=10,
            border_width=1, border_color=BORDER,
            font=ctk.CTkFont(family="Consolas", size=12))
        self.dash_info.grid(row=1, column=0, padx=14, pady=(0, 6), sticky="nsew")
        self._button(info, "Refresh", self.refresh_dash_info, width=110,
                     fg_color=FIELD, hover_color=BORDER).grid(
            row=2, column=0, padx=14, pady=(0, 14), sticky="w")

        bottom = self._card(parent)
        bottom.grid(row=4, column=0, padx=16, pady=(6, 14), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self.dash_status_label = ctk.CTkLabel(bottom, text="Ready.", anchor="w",
                                             text_color=TEXT)
        self.dash_status_label.grid(row=0, column=0, padx=14, pady=(12, 12),
                                    sticky="ew")

        self.refresh_dash_info()

    def refresh_dash_info(self):
        """Show what the store holds, so an empty dashboard is never a surprise."""
        self.dash_info.delete("1.0", "end")
        path = default_store_path()
        if self.rpt_store is None and not os.path.exists(path):
            self.dash_info.insert(
                "end",
                "No local store yet.\n\n"
                "Go to the Reporting tab, connect with your Reporting API\n"
                "client credentials and run an extract first - the dashboard\n"
                "is built from whatever that puts in the store.\n\n"
                f"Store will be created at:\n  {path}\n")
            return
        try:
            store = self.rpt_store or ReportStore(path)
            counts = store.counts()
            runs = store.last_runs()
            if self.rpt_store is None:
                store.close()
        except Exception as exc:
            self.dash_info.insert("end", f"Could not open the store: {exc}\n")
            return

        lines = [f"Store: {path}", ""]
        live = {t: n for t, n in sorted(counts.items()) if n}
        if live:
            lines.append("Rows:")
            width = max(len(t) for t in live)
            for table, n in live.items():
                lines.append(f"  {table.ljust(width)}  {n:>8,}")
        else:
            lines.append("Rows: (store is empty - run an extract first)")
        if runs:
            lines += ["", "Last extract per entity:"]
            width = max(len(e) for e in runs)
            for entity, run in sorted(runs.items()):
                lines.append(
                    f"  {entity.ljust(width)}  {run['started_at'] or '-'}  "
                    f"{run['mode']}  rows={run['rows']}  "
                    f"window={run['date_range'] or '-'}")
        self.dash_info.insert("end", "\n".join(lines) + "\n")

    def on_dash_build(self):
        path = default_store_path()
        if self.rpt_store is None and not os.path.exists(path):
            self.dash_status_label.configure(
                text="No store yet - run an extract on the Reporting tab first.")
            return
        title = self.dash_title_entry.get().strip() or "Skynamo Dashboard"
        period_label = self.dash_label_entry.get().strip()
        default = (f"skynamo_dashboard_"
                   f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        out_path = filedialog.asksaveasfilename(
            defaultextension=".html", initialfile=default,
            filetypes=[("HTML files", "*.html"), ("All files", "*.*")])
        if not out_path:
            return
        try:
            store = self.rpt_store or ReportStore(path)
            build_dashboard(store, out_path, title=title,
                            period_label=period_label)
            if self.rpt_store is None:
                store.close()
        except Exception as exc:
            self.dash_status_label.configure(text=f"Build failed: {exc}")
            return
        self.dash_last_path = out_path
        self.dash_open_btn.configure(state="normal")
        self.dash_status_label.configure(text=f"Dashboard written to {out_path}")

    def on_dash_open(self):
        if not self.dash_last_path:
            return
        webbrowser.open(f"file:///{self.dash_last_path.replace(os.sep, '/')}")
        self.dash_status_label.configure(
            text=f"Opened {self.dash_last_path} in your browser.")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
