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
    STATUS_PENDING,
)
from skynamo_geo.customers import build_address, collect_custom_field_names
from skynamo_geo.geocoder import GoogleGeocoder, GeocodeError

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

CHECK_ON = "☑"   # ballot box with check
CHECK_OFF = "☐"  # empty ballot box


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Skynamo Geolocation Updater")
        self.geometry("1040x760")
        self.minsize(900, 640)

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
        self.grid_rowconfigure(2, weight=1)

        # ----- Top: connection + mapping side by side -----
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        # Connection panel
        conn = ctk.CTkFrame(top)
        conn.grid(row=0, column=0, padx=(0, 6), pady=0, sticky="nsew")
        ctk.CTkLabel(conn, text="1. Connection",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=2, padx=10, pady=(8, 4), sticky="w")

        self.instance_entry = self._labeled_entry(conn, "Instance name", 1)
        self.skynamo_entry = self._labeled_entry(conn, "Skynamo API key", 2, show="*")
        self.google_entry = self._labeled_entry(conn, "Google Maps API key", 3, show="*")
        self.country_entry = self._labeled_entry(
            conn, "Country (2-letter, optional)", 4)

        self.remember_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(conn, text="Remember credentials & settings",
                        variable=self.remember_var).grid(
            row=5, column=0, columnspan=2, padx=10, pady=4, sticky="w")

        self.connect_btn = ctk.CTkButton(
            conn, text="Connect & Load Customers", command=self.on_connect)
        self.connect_btn.grid(row=6, column=0, columnspan=2,
                              padx=10, pady=(4, 10), sticky="ew")

        # Mapping panel
        mapping = ctk.CTkFrame(top)
        mapping.grid(row=0, column=1, padx=(6, 0), pady=0, sticky="nsew")
        mapping.grid_rowconfigure(1, weight=1)
        mapping.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(mapping, text="2. Map address field(s)",
                     font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=10, pady=(8, 4), sticky="w")

        self.fields_frame = ctk.CTkScrollableFrame(mapping, height=140)
        self.fields_frame.grid(row=1, column=0, padx=10, pady=4, sticky="nsew")
        self._fields_placeholder()

        self.sample_label = ctk.CTkLabel(
            mapping, text="Sample address: -", anchor="w",
            wraplength=460, justify="left")
        self.sample_label.grid(row=2, column=0, padx=10, pady=2, sticky="ew")

        self.replace_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(mapping,
                        text="Replace coordinates that already exist",
                        variable=self.replace_var).grid(
            row=3, column=0, padx=10, pady=(2, 10), sticky="w")

        # ----- Middle: action bar -----
        actions = ctk.CTkFrame(self)
        actions.grid(row=1, column=0, padx=12, pady=6, sticky="ew")
        self.preview_btn = ctk.CTkButton(
            actions, text="Preview (geocode only)", command=self.on_preview,
            state="disabled")
        self.preview_btn.pack(side="left", padx=6, pady=8)
        self.write_btn = ctk.CTkButton(
            actions, text="Write Selected to Skynamo", command=self.on_write,
            state="disabled", fg_color="#2e7d32", hover_color="#1b5e20")
        self.write_btn.pack(side="left", padx=6, pady=8)
        self.cancel_btn = ctk.CTkButton(
            actions, text="Cancel", command=self.on_cancel, state="disabled",
            fg_color="#b71c1c", hover_color="#7f0000")
        self.cancel_btn.pack(side="left", padx=6, pady=8)
        self.save_btn = ctk.CTkButton(
            actions, text="Save Report CSV", command=self.on_save_report,
            state="disabled")
        self.save_btn.pack(side="left", padx=6, pady=8)

        self.select_all_btn = ctk.CTkButton(
            actions, text="Select all", width=90,
            command=lambda: self._set_all_includes(True), state="disabled")
        self.select_all_btn.pack(side="right", padx=6, pady=8)
        self.select_none_btn = ctk.CTkButton(
            actions, text="Select none", width=90,
            command=lambda: self._set_all_includes(False), state="disabled")
        self.select_none_btn.pack(side="right", padx=6, pady=8)

        # ----- Results table -----
        table_frame = ctk.CTkFrame(self)
        table_frame.grid(row=2, column=0, padx=12, pady=6, sticky="nsew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)

        columns = ("include", "name", "address", "lat", "lng",
                   "precision", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
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
        self.tree.tag_configure("low", background="#fff3cd")
        self.tree.tag_configure("skip", foreground="#888888")
        self.tree.tag_configure("fail", background="#f8d7da")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical",
                                command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self._on_tree_click)

        # ----- Bottom: progress + log + summary -----
        bottom = ctk.CTkFrame(self)
        bottom.grid(row=3, column=0, padx=12, pady=(6, 12), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(bottom)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        self.status_label = ctk.CTkLabel(bottom, text="Ready.", anchor="w")
        self.status_label.grid(row=1, column=0, padx=10, pady=2, sticky="ew")
        self.log = ctk.CTkTextbox(bottom, height=120)
        self.log.grid(row=2, column=0, padx=10, pady=(4, 10), sticky="ew")

    def _labeled_entry(self, parent, label, row, show=None):
        ctk.CTkLabel(parent, text=label, anchor="w").grid(
            row=row, column=0, padx=(10, 4), pady=3, sticky="w")
        entry = ctk.CTkEntry(parent, show=show, width=260)
        entry.grid(row=row, column=1, padx=(0, 10), pady=3, sticky="ew")
        parent.grid_columnconfigure(1, weight=1)
        return entry

    def _fields_placeholder(self):
        for child in self.fields_frame.winfo_children():
            child.destroy()
        ctk.CTkLabel(self.fields_frame,
                     text="Connect & load customers to list fields.").pack(
            anchor="w", padx=6, pady=6)

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
        # Secrets
        google = settings.get_secret(settings.GOOGLE_KEY_NAME)
        if google:
            self.google_entry.insert(0, google)
        if cfg.get("instance_name"):
            sk = settings.get_secret(
                settings.skynamo_key_name(cfg["instance_name"]))
            if sk:
                self.skynamo_entry.insert(0, sk)

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
        google_key = self.google_entry.get().strip()
        country = self.country_entry.get().strip().upper()
        if not (instance and skynamo_key and google_key):
            self.set_status("Enter instance, Skynamo key and Google key first.")
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
                geocoder = GoogleGeocoder(google_key)
                self.queue.put(("status", "Validating Google Maps key..."))
                geocoder.validate(country=self.country)
                self.queue.put(("log", "Google Maps key OK."))
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
                         text="No custom fields found on customers.").pack(
                anchor="w", padx=6, pady=6)
            return
        saved = set(getattr(self, "_saved_fields", []) or [])
        for name in names:
            var = ctk.BooleanVar(value=name in saved)
            var.trace_add("write", lambda *_: self._update_sample())
            ctk.CTkCheckBox(self.fields_frame, text=name, variable=var).pack(
                anchor="w", padx=6, pady=2)
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
