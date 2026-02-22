"""Zigbee Manager - Zigbee2MQTT device management with GTK4/Adwaita."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio
import json
import threading
import gettext
from datetime import datetime
from zigbee_manager.accessibility import AccessibilityManager

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False

_ = gettext.gettext
APP_ID = "io.github.yeager.ZigbeeManager"



def _wlc_settings_path():
    import os
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = os.path.join(xdg, "zigbee-manager")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "welcome.json")

def _load_wlc_settings():
    import os, json
    p = _wlc_settings_path()
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"welcome_shown": False}

def _save_wlc_settings(s):
    import json
    with open(_wlc_settings_path(), "w") as f:
        json.dump(s, f, indent=2)

class DeviceRow(Gtk.ListBoxRow):
    def __init__(self, device):
        super().__init__()
        self.device = device
        name = device.get("friendly_name", device.get("ieee_address", "?"))
        model = device.get("definition", {}).get("model", "Unknown") if device.get("definition") else "Coordinator"
        vendor = device.get("definition", {}).get("vendor", "") if device.get("definition") else ""
        dtype = device.get("type", "")
        ieee = device.get("ieee_address", "")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                       margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)

        # Icon based on type
        icon_name = "network-server-symbolic"
        if dtype == "EndDevice":
            icon_name = "preferences-system-symbolic"
        elif dtype == "Router":
            icon_name = "network-transmit-receive-symbolic"
        elif dtype == "Coordinator":
            icon_name = "network-server-symbolic"
        box.append(Gtk.Image(icon_name=icon_name, pixel_size=32))

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.append(Gtk.Label(label=name, xalign=0, css_classes=["heading"]))
        detail = f"{vendor} {model}" if vendor else model
        info_box.append(Gtk.Label(label=detail, xalign=0, css_classes=["dim-label", "caption"]))
        info_box.append(Gtk.Label(label=f"{dtype} | {ieee}", xalign=0, css_classes=["dim-label", "caption", "monospace"]))
        box.append(info_box)

        # LQI / power source
        ps = device.get("power_source", "")
        if ps:
            box.append(Gtk.Label(label=ps, css_classes=["dim-label"]))

        self.set_child(box)


class ZigbeeManagerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, title=_("Zigbee Manager"), default_width=1000, default_height=700)
        self.devices = []
        self.base_url = "http://localhost:8080"
        self.mqtt_client = None

        header = Adw.HeaderBar()
        self.theme_btn = Gtk.Button(icon_name="weather-clear-night-symbolic")
        self.theme_btn.connect("clicked", self._toggle_theme)
        header.pack_end(self.theme_btn)
        about_btn = Gtk.Button(icon_name="help-about-symbolic")
        about_btn.connect("clicked", self._show_about)
        header.pack_end(about_btn)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh_btn.connect("clicked", lambda _: self._fetch_devices())
        header.pack_start(refresh_btn)

        # Connection settings
        conn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                           margin_start=12, margin_end=12, margin_top=8)
        conn_box.append(Gtk.Label(label=_("Z2M URL:")))
        self.url_entry = Gtk.Entry(text=self.base_url, hexpand=True)
        conn_box.append(self.url_entry)
        conn_btn = Gtk.Button(label=_("Connect"), css_classes=["suggested-action"])
        conn_btn.connect("clicked", self._connect)
        conn_box.append(conn_btn)
        self.conn_label = Gtk.Label(label="", css_classes=["dim-label"])
        conn_box.append(self.conn_label)

        # Split pane
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)

        # Device list
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_box.set_size_request(350, -1)

        search_entry = Gtk.SearchEntry(placeholder_text=_("Search devices..."),
                                        margin_start=8, margin_end=8, margin_top=8)
        search_entry.connect("search-changed", self._on_search)
        left_box.append(search_entry)
        self._search_text = ""

        sw = Gtk.ScrolledWindow(vexpand=True)
        self.device_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.device_list.set_filter_func(self._filter_func)
        self.device_list.connect("row-selected", self._on_device_selected)
        sw.set_child(self.device_list)
        left_box.append(sw)

        split.set_start_child(left_box)

        # Detail panel
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                             margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        right_box.set_hexpand(True)

        self.detail_title = Gtk.Label(label=_("Select a device"), css_classes=["title-2"], xalign=0)
        right_box.append(self.detail_title)

        # Device info
        info_frame = Gtk.Frame(label=_("Device Info"))
        self.info_grid = Gtk.Grid(row_spacing=6, column_spacing=12,
                                   margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        info_frame.set_child(self.info_grid)
        right_box.append(info_frame)

        # Actions
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.rename_entry = Gtk.Entry(placeholder_text=_("New name..."), hexpand=True)
        actions_box.append(self.rename_entry)
        rename_btn = Gtk.Button(label=_("Rename"))
        rename_btn.connect("clicked", self._rename_device)
        actions_box.append(rename_btn)
        right_box.append(actions_box)

        actions2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ota_btn = Gtk.Button(label=_("Check OTA Update"), css_classes=["suggested-action"])
        ota_btn.connect("clicked", self._check_ota)
        actions2.append(ota_btn)
        remove_btn = Gtk.Button(label=_("Remove Device"), css_classes=["destructive-action"])
        remove_btn.connect("clicked", self._remove_device)
        actions2.append(remove_btn)
        permit_btn = Gtk.Button(label=_("Permit Join (60s)"))
        permit_btn.connect("clicked", self._permit_join)
        actions2.append(permit_btn)
        right_box.append(actions2)

        # Network tree view
        net_frame = Gtk.Frame(label=_("Network Mesh (Tree View)"))
        sw2 = Gtk.ScrolledWindow(vexpand=True, min_content_height=200)
        self.net_store = Gtk.TreeStore(str, str, str)  # name, type, ieee
        self.net_tree = Gtk.TreeView(model=self.net_store, headers_visible=True)
        for i, title in enumerate([_("Device"), _("Type"), _("IEEE Address")]):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_resizable(True)
            if i == 0:
                col.set_expand(True)
            self.net_tree.append_column(col)
        sw2.set_child(self.net_tree)
        net_frame.set_child(sw2)
        right_box.append(net_frame)

        split.set_end_child(right_box)
        split.set_position(350)

        self.statusbar = Gtk.Label(label="", xalign=0, css_classes=["dim-label"], margin_start=12, margin_bottom=4)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(header)
        content.append(conn_box)
        content.append(split)
        content.append(self.statusbar)
        self.set_content(content)

        self._selected_device = None
        GLib.timeout_add_seconds(1, self._update_status)

    def _connect(self, _btn=None):
        self.base_url = self.url_entry.get_text().strip().rstrip("/")
        self._fetch_devices()

    def _fetch_devices(self):
        if not HAS_REQUESTS:
            self.conn_label.set_label(_("requests not installed!"))
            return

        def do_fetch():
            try:
                resp = requests.get(f"{self.base_url}/api/devices", timeout=5)
                devices = resp.json()
                GLib.idle_add(self._populate_devices, devices)
            except Exception as e:
                GLib.idle_add(self.conn_label.set_label, f"Error: {e}")

        threading.Thread(target=do_fetch, daemon=True).start()
        self.conn_label.set_label(_("Fetching..."))

    def _populate_devices(self, devices):
        self.devices = devices
        # Clear list
        while True:
            row = self.device_list.get_row_at_index(0)
            if row is None:
                break
            self.device_list.remove(row)

        for dev in sorted(devices, key=lambda d: d.get("friendly_name", "")):
            self.device_list.append(DeviceRow(dev))

        self.conn_label.set_label(f"{len(devices)} devices")
        self._build_network_tree(devices)

    def _build_network_tree(self, devices):
        self.net_store.clear()
        # Build a simple tree: Coordinator → Routers → EndDevices
        coord = None
        routers = {}
        endpoints = []

        for dev in devices:
            dtype = dev.get("type", "")
            name = dev.get("friendly_name", dev.get("ieee_address", "?"))
            ieee = dev.get("ieee_address", "")
            if dtype == "Coordinator":
                coord = self.net_store.append(None, [name, dtype, ieee])
            elif dtype == "Router":
                it = self.net_store.append(None if not coord else coord, [name, dtype, ieee])
                routers[ieee] = it
            else:
                endpoints.append(dev)

        if not coord:
            coord = self.net_store.append(None, ["Coordinator", "Coordinator", ""])

        for dev in endpoints:
            name = dev.get("friendly_name", dev.get("ieee_address", "?"))
            ieee = dev.get("ieee_address", "")
            # Attach to a router if possible, else to coordinator
            parent = coord
            self.net_store.append(parent, [name, dev.get("type", ""), ieee])

        self.net_tree.expand_all()

    def _on_device_selected(self, listbox, row):
        if row is None:
            return
        dev = row.device
        self._selected_device = dev
        name = dev.get("friendly_name", "?")
        self.detail_title.set_label(name)
        self.rename_entry.set_text(name)

        # Clear grid
        while True:
            child = self.info_grid.get_child_at(0, 0)
            if child is None:
                break
            self.info_grid.remove(child)

        fields = [
            (_("IEEE Address"), dev.get("ieee_address", "")),
            (_("Type"), dev.get("type", "")),
            (_("Model"), dev.get("definition", {}).get("model", "N/A") if dev.get("definition") else "N/A"),
            (_("Vendor"), dev.get("definition", {}).get("vendor", "N/A") if dev.get("definition") else "N/A"),
            (_("Power Source"), dev.get("power_source", "N/A")),
            (_("Software Build"), dev.get("software_build_id", "N/A")),
            (_("Date Code"), dev.get("date_code", "N/A")),
            (_("Interview Completed"), str(dev.get("interview_completed", "N/A"))),
        ]
        for i, (label, value) in enumerate(fields):
            self.info_grid.attach(Gtk.Label(label=label, xalign=0, css_classes=["dim-label"]), 0, i, 1, 1)
            self.info_grid.attach(Gtk.Label(label=str(value), xalign=0, selectable=True, css_classes=["monospace"]), 1, i, 1, 1)

    def _filter_func(self, row):
        if not self._search_text:
            return True
        dev = row.device
        name = dev.get("friendly_name", "").lower()
        model = (dev.get("definition", {}) or {}).get("model", "").lower()
        return self._search_text in name or self._search_text in model

    def _on_search(self, entry):
        self._search_text = entry.get_text().lower()
        self.device_list.invalidate_filter()

    def _rename_device(self, _btn):
        if not self._selected_device or not HAS_REQUESTS:
            return
        old = self._selected_device.get("friendly_name", "")
        new = self.rename_entry.get_text().strip()
        if not new or new == old:
            return
        try:
            requests.post(f"{self.base_url}/api/device/{old}/rename",
                         json={"new_name": new}, timeout=5)
            self._fetch_devices()
        except Exception as e:
            self.conn_label.set_label(f"Rename error: {e}")

    def _check_ota(self, _btn):
        if not self._selected_device or not HAS_REQUESTS:
            return
        name = self._selected_device.get("friendly_name", "")
        try:
            resp = requests.post(f"{self.base_url}/api/device/{name}/ota_update/check", timeout=10)
            self.conn_label.set_label(f"OTA: {resp.text[:100]}")
        except Exception as e:
            self.conn_label.set_label(f"OTA error: {e}")

    def _remove_device(self, _btn):
        if not self._selected_device or not HAS_REQUESTS:
            return
        name = self._selected_device.get("friendly_name", "")
        try:
            requests.post(f"{self.base_url}/api/device/{name}/remove", timeout=10)
            self._fetch_devices()
        except Exception as e:
            self.conn_label.set_label(f"Remove error: {e}")

    def _permit_join(self, _btn):
        if not HAS_REQUESTS:
            return
        try:
            requests.post(f"{self.base_url}/api/permit_join", json={"value": True, "time": 60}, timeout=5)
            self.conn_label.set_label(_("Permit join enabled (60s)"))
        except Exception as e:
            self.conn_label.set_label(f"Error: {e}")

    def _update_status(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        n = len(self.devices)
        self.statusbar.set_label(f"  {n} devices | {now}")
        return True

    def _toggle_theme(self, _btn):
        mgr = Adw.StyleManager.get_default()
        if mgr.get_dark():
            mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        else:
            mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

    def _show_about(self, _btn):
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="Zigbee Manager",
            application_icon="network-wireless-symbolic",
            version="0.1.0",
            developer_name="Daniel Nylander",
            developers=["Daniel Nylander"],
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/yeager/zigbee-manager",
            issue_url="https://github.com/yeager/zigbee-manager/issues",
            translator_credits=_("translator-credits"),
            comments=_("Zigbee device management via Zigbee2MQTT"),
        )
        about.add_link(_("Translations"), "https://www.transifex.com/danielnylander/zigbee-manager")
        about.present(self)


class ZigbeeManagerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window or ZigbeeManagerWindow(application=self)
        win.present()
        # Welcome dialog
        self._wlc_settings = _load_wlc_settings()
        if not self._wlc_settings.get("welcome_shown"):
            self._show_welcome(self.props.active_window or self)


    def do_startup(self):
        Adw.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])


def main():
    app = ZigbeeManagerApp()
    app.run()


if __name__ == "__main__":
    main()

    def _show_welcome(self, win):
        dialog = Adw.Dialog()
        dialog.set_title(_("Welcome"))
        dialog.set_content_width(420)
        dialog.set_content_height(480)
        page = Adw.StatusPage()
        page.set_icon_name("network-wireless-symbolic")
        page.set_title(_("Welcome to Zigbee Manager"))
        page.set_description(_("Manage Zigbee smart home devices.\n\n✓ Discover and pair devices\n✓ View device status\n✓ Network topology"))
        btn = Gtk.Button(label=_("Get Started"))
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.CENTER)
        btn.set_margin_top(12)
        btn.connect("clicked", self._on_welcome_close, dialog)
        page.set_child(btn)
        box = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.set_show_title(False)
        box.add_top_bar(hb)
        box.set_content(page)
        dialog.set_child(box)
        dialog.present(win)

    def _on_welcome_close(self, btn, dialog):
        self._wlc_settings["welcome_shown"] = True
        _save_wlc_settings(self._wlc_settings)
        dialog.close()



# --- Session restore ---
import json as _json
import os as _os

def _save_session(window, app_name):
    config_dir = _os.path.join(_os.path.expanduser('~'), '.config', app_name)
    _os.makedirs(config_dir, exist_ok=True)
    state = {'width': window.get_width(), 'height': window.get_height(),
             'maximized': window.is_maximized()}
    try:
        with open(_os.path.join(config_dir, 'session.json'), 'w') as f:
            _json.dump(state, f)
    except OSError:
        pass

def _restore_session(window, app_name):
    path = _os.path.join(_os.path.expanduser('~'), '.config', app_name, 'session.json')
    try:
        with open(path) as f:
            state = _json.load(f)
        window.set_default_size(state.get('width', 800), state.get('height', 600))
        if state.get('maximized'):
            window.maximize()
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        pass


# --- Fullscreen toggle (F11) ---
def _setup_fullscreen(window, app):
    """Add F11 fullscreen toggle."""
    from gi.repository import Gio
    if not app.lookup_action('toggle-fullscreen'):
        action = Gio.SimpleAction.new('toggle-fullscreen', None)
        action.connect('activate', lambda a, p: (
            window.unfullscreen() if window.is_fullscreen() else window.fullscreen()
        ))
        app.add_action(action)
        app.set_accels_for_action('app.toggle-fullscreen', ['F11'])


# --- Plugin system ---
import importlib.util
import os as _pos

def _load_plugins(app_name):
    """Load plugins from ~/.config/<app>/plugins/."""
    plugin_dir = _pos.path.join(_pos.path.expanduser('~'), '.config', app_name, 'plugins')
    plugins = []
    if not _pos.path.isdir(plugin_dir):
        return plugins
    for fname in sorted(_pos.listdir(plugin_dir)):
        if fname.endswith('.py') and not fname.startswith('_'):
            path = _pos.path.join(plugin_dir, fname)
            try:
                spec = importlib.util.spec_from_file_location(fname[:-3], path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                plugins.append(mod)
            except Exception as e:
                print(f"Plugin {fname}: {e}")
    return plugins
