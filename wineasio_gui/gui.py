"""The GTK 4 / libadwaita window."""

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__, config, pipewire, wine  # noqa: E402

PREFERRED_SIZES = [size for size in pipewire.BUFFER_SIZES
                   if wine.BUFFER_MIN <= size <= wine.BUFFER_MAX]


def in_background(work, done):
    """Run work() on a thread and done(result, error) back on the main loop."""
    def target():
        try:
            result, error = work(), None
        except Exception as exc:  # reported to the user as a toast
            result, error = None, exc
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=target, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="WineASIO", default_width=560,
                         default_height=820)
        self.loading = False
        self.save_source = None
        self.status_busy = False
        self.client_rows = []
        self.prefixes = []

        self.toasts = Adw.ToastOverlay()
        self.page = Adw.PreferencesPage()
        self.toasts.set_child(self.page)

        view = Adw.ToolbarView(content=self.toasts)
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Refresh", "app.refresh")
        menu.append("About WineASIO Settings", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        view.add_top_bar(header)
        overrides = wine.environment_overrides()
        if overrides:
            view.add_top_bar(Adw.Banner(
                revealed=True,
                title=f"{', '.join(overrides)} set in this session override the prefix settings below"))
        self.set_content(view)

        self.build_audio()
        self.build_running()
        self.build_prefix()
        self.build_driver_settings()
        self.build_system()

        self.reload_prefixes()
        self.refresh_audio()
        self.refresh_status()
        GLib.timeout_add_seconds(3, self.on_tick)

    # -- layout -------------------------------------------------------------

    def build_audio(self):
        group = Adw.PreferencesGroup(
            title="Audio",
            description="For every Wine app using WineASIO. Apps pick changes up the next "
                        "time they open their audio device.")
        low, high = pipewire.quantum_limits()
        self.buffer_sizes = [None] + [s for s in pipewire.BUFFER_SIZES if low <= s <= high]
        self.buffer_row = Adw.ComboRow(title="Buffer size",
                                       model=Gtk.StringList.new(["PipeWire default"]))
        self.buffer_row.connect("notify::selected", self.on_buffer)
        group.add(self.buffer_row)
        self.rates = [None] + pipewire.RATES
        self.rate_row = Adw.ComboRow(title="Sample rate",
                                     model=Gtk.StringList.new(["System default"]))
        self.rate_row.connect("notify::selected", self.on_rate)
        group.add(self.rate_row)
        self.page.add(group)

    def build_running(self):
        self.running_group = Adw.PreferencesGroup(title="Running")
        self.idle_row = Adw.ActionRow(title="No ASIO apps running",
                                      subtitle="Apps show up here while WineASIO is open")
        self.running_group.add(self.idle_row)
        self.page.add(self.running_group)

    def build_prefix(self):
        group = Adw.PreferencesGroup(
            title="Wine prefix",
            description="Channel and driver settings are stored in each prefix.")
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a prefix",
                         css_classes=["flat"])
        add.connect("clicked", self.on_add_prefix)
        group.set_header_suffix(add)
        self.prefix_row = Adw.ComboRow(title="Prefix", model=Gtk.StringList.new([]))
        self.prefix_row.connect("notify::selected", self.on_prefix)
        group.add(self.prefix_row)
        self.registered_row = Adw.ActionRow(title="WineASIO in this prefix")
        self.register_button = Gtk.Button(label="Register", valign=Gtk.Align.CENTER,
                                          css_classes=["suggested-action"])
        self.register_button.connect("clicked", self.on_register)
        self.registered_row.add_suffix(self.register_button)
        group.add(self.registered_row)
        self.page.add(group)

    def build_driver_settings(self):
        self.channels_group = Adw.PreferencesGroup(title="Channels")
        self.inputs_row = Adw.SpinRow.new_with_range(0, 128, 1)
        self.inputs_row.set_title("Inputs")
        self.outputs_row = Adw.SpinRow.new_with_range(0, 128, 1)
        self.outputs_row.set_title("Outputs")
        for row in (self.inputs_row, self.outputs_row):
            row.connect("notify::value", self.on_setting)
            self.channels_group.add(row)
        self.page.add(self.channels_group)

        self.behaviour_group = Adw.PreferencesGroup(title="Driver")
        self.connect_row = Adw.SwitchRow(
            title="Connect to hardware",
            subtitle="Link the channels to your interface's inputs and outputs")
        self.fixed_row = Adw.SwitchRow(
            title="Follow PipeWire's buffer size",
            subtitle="Recommended. When off, apps may ask for their own size")
        self.preferred_row = Adw.ComboRow(
            title="Preferred buffer size",
            subtitle="Used only when not following PipeWire",
            model=Gtk.StringList.new([f"{s} samples" for s in PREFERRED_SIZES]))
        self.autostart_row = Adw.SwitchRow(
            title="Start a JACK server if none runs",
            subtitle="Not needed with PipeWire")
        for row in (self.connect_row, self.fixed_row, self.autostart_row):
            row.connect("notify::active", self.on_setting)
        self.preferred_row.connect("notify::selected", self.on_setting)
        for row in (self.connect_row, self.fixed_row, self.preferred_row, self.autostart_row):
            self.behaviour_group.add(row)
        self.page.add(self.behaviour_group)

    def build_system(self):
        group = Adw.PreferencesGroup(title="System")
        drivers = wine.installed_drivers()
        self.page_driver = Adw.ActionRow(
            title="WineASIO driver",
            subtitle="\n".join(f"{arch}: {path}" for path, arch in drivers)
            or "Not found in any Wine library directory",
            subtitle_lines=3)
        self.page_driver.add_prefix(self.state_icon(bool(drivers)))
        group.add(self.page_driver)

        library, ours = pipewire.jack_library()
        jack = Adw.ActionRow(
            title="JACK library",
            subtitle=(f"PipeWire's ({library})" if ours else
                      f"{library} is not PipeWire's: WineASIO needs a running JACK server"
                      if library else "libjack.so.0 is missing: install pipewire-jack"),
            subtitle_lines=2)
        jack.add_prefix(self.state_icon(ours))
        group.add(jack)

        limit = pipewire.memlock_kib()
        fine = limit is None or limit >= 1024 * 1024
        memlock = Adw.ActionRow(
            title="Memory lock limit",
            subtitle=("Unlimited" if limit is None else f"{limit // 1024} MiB")
            + ("" if fine else ". Stock WineASIO can hang when it opens with a limit this "
                               "small; see the README for the fix"),
            subtitle_lines=3)
        memlock.add_prefix(self.state_icon(fine))
        group.add(memlock)
        self.page.add(group)

    @staticmethod
    def state_icon(good):
        icon = Gtk.Image(icon_name="object-select-symbolic" if good else "dialog-warning-symbolic")
        icon.add_css_class("success" if good else "warning")
        return icon

    def toast(self, text, timeout=4):
        toast = Adw.Toast(title=text)
        toast.set_timeout(timeout)
        self.toasts.add_toast(toast)

    # -- audio --------------------------------------------------------------

    def refresh_audio(self):
        rate = pipewire.graph_rate()
        self.loading = True
        self.buffer_row.set_model(Gtk.StringList.new(
            ["PipeWire default"] + [f"{s} samples, {pipewire.latency_ms(s, rate):.1f} ms"
                                    for s in self.buffer_sizes[1:]]))
        current = pipewire.buffer_size()
        self.buffer_row.set_selected(self.buffer_sizes.index(current)
                                     if current in self.buffer_sizes else 0)
        self.rate_row.set_model(Gtk.StringList.new(
            ["System default"]
            + [f"{r} Hz" for r in self.rates[1:]]))
        pinned = pipewire.sample_rate()
        self.rate_row.set_selected(self.rates.index(pinned) if pinned in self.rates else 0)
        self.rate_row.set_subtitle(f"PipeWire runs at {rate} Hz")
        self.loading = False

    def on_buffer(self, row, _):
        if self.loading:
            return
        size = self.buffer_sizes[row.get_selected()]
        pipewire.set_buffer_size(size)
        self.toast("Buffer size saved. Reopen the audio device in your app to use it.")

    def on_rate(self, row, _):
        if self.loading:
            return
        rate = self.rates[row.get_selected()]
        pipewire.set_sample_rate(rate)
        GLib.timeout_add(600, lambda: self.refresh_audio() and False)
        self.toast("Sample rate set. Reopen the audio device in your app to use it.")

    # -- running clients -----------------------------------------------------

    def on_tick(self):
        if self.get_visible():
            self.refresh_status()
        return True

    def refresh_status(self):
        if self.status_busy:
            return
        self.status_busy = True
        in_background(pipewire.clients, self.show_status)

    def show_status(self, clients, error):
        self.status_busy = False
        for row in self.client_rows:
            self.running_group.remove(row)
        self.client_rows = []
        self.idle_row.set_visible(not clients)
        for client in clients or []:
            parts = []
            if client.buffer and client.rate:
                parts.append(f"{client.buffer} samples at {client.rate} Hz "
                             f"({pipewire.latency_ms(client.buffer, client.rate):.1f} ms)")
            if client.xruns is not None:
                parts.append(f"{client.xruns} xruns" if client.xruns != 1 else "1 xrun")
            row = Adw.ActionRow(title=client.name, subtitle=", ".join(parts))
            row.add_prefix(Gtk.Image(icon_name="audio-x-generic-symbolic"))
            self.running_group.add(row)
            self.client_rows.append(row)

    # -- prefixes -----------------------------------------------------------

    def reload_prefixes(self, select=None):
        self.prefixes = wine.discover(config.added_prefixes())
        self.loading = True
        self.prefix_row.set_model(Gtk.StringList.new([p.label for p in self.prefixes]))
        wanted = select or config.last_prefix()
        index = next((i for i, p in enumerate(self.prefixes) if wanted and p.path == wanted), 0)
        if self.prefixes:
            self.prefix_row.set_selected(index)
        self.loading = False
        self.show_prefix()

    @property
    def prefix(self):
        index = self.prefix_row.get_selected()
        return self.prefixes[index] if 0 <= index < len(self.prefixes) else None

    def on_prefix(self, *_):
        if self.loading:
            return
        if self.prefix:
            config.set_last_prefix(self.prefix.path)
        self.show_prefix()

    def show_prefix(self):
        prefix = self.prefix
        for group in (self.channels_group, self.behaviour_group):
            group.set_sensitive(prefix is not None)
        self.registered_row.set_sensitive(prefix is not None)
        if prefix is None:
            self.prefix_row.set_subtitle("No Wine prefixes found; add one with +")
            self.registered_row.set_subtitle("")
            return
        self.prefix_row.set_subtitle(str(prefix.path))
        registered = prefix.registered
        self.registered_row.set_subtitle(
            "Registered" if registered else "Not registered: apps won't list WineASIO yet")
        self.register_button.set_visible(not registered)
        settings = prefix.settings()
        self.loading = True
        self.inputs_row.set_value(settings.inputs)
        self.outputs_row.set_value(settings.outputs)
        self.connect_row.set_active(settings.connect_to_hardware)
        self.fixed_row.set_active(settings.fixed_buffersize)
        self.autostart_row.set_active(settings.autostart_server)
        preferred = settings.preferred_buffersize
        self.preferred_row.set_selected(PREFERRED_SIZES.index(preferred)
                                        if preferred in PREFERRED_SIZES else
                                        PREFERRED_SIZES.index(1024))
        self.preferred_row.set_sensitive(not settings.fixed_buffersize)
        self.loading = False

    def on_add_prefix(self, _button):
        dialog = Gtk.FileDialog(title="Choose a Wine prefix")
        dialog.select_folder(self, None, self.on_prefix_chosen)

    def on_prefix_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        path = Path(folder.get_path())
        if not wine.Prefix(path, "").is_prefix():
            self.toast(f"{path.name} is not a Wine prefix (no system.reg)")
            return
        config.add_prefix(path)
        config.set_last_prefix(path)
        self.reload_prefixes(select=path)
        self.toast(f"Added {path.name}")

    def on_register(self, _button):
        prefix = self.prefix
        self.register_button.set_sensitive(False)
        self.registered_row.set_subtitle("Registering...")

        def done(result, error):
            self.register_button.set_sensitive(True)
            if error or not result:
                self.toast("Registration failed. Is WineASIO installed for this prefix's Wine?",
                           timeout=6)
            else:
                self.toast(f"Registered {', '.join(result)}")
            self.show_prefix()
        in_background(prefix.register, done)

    # -- saving ---------------------------------------------------------------

    def on_setting(self, *_):
        if self.loading or self.prefix is None:
            return
        self.preferred_row.set_sensitive(not self.fixed_row.get_active())
        if self.save_source:
            GLib.source_remove(self.save_source)
        self.save_source = GLib.timeout_add(500, self.save)

    def save(self):
        self.save_source = None
        prefix = self.prefix
        settings = wine.Settings(
            inputs=int(self.inputs_row.get_value()),
            outputs=int(self.outputs_row.get_value()),
            connect_to_hardware=self.connect_row.get_active(),
            fixed_buffersize=self.fixed_row.get_active(),
            preferred_buffersize=PREFERRED_SIZES[self.preferred_row.get_selected()],
            autostart_server=self.autostart_row.get_active())

        def done(_result, error):
            if error:
                self.toast(f"Could not save: {error}", timeout=6)
            else:
                self.toast("Saved. Reopen WineASIO in your app to use it.")
        in_background(lambda: prefix.save(settings), done)
        return False


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        for name, callback, accels in (("refresh", self.on_refresh, ["<Control>r", "F5"]),
                                       ("about", self.on_about, []),
                                       ("quit", lambda *_: self.quit(), ["<Control>q"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()
        # Don't let the first spin button take focus and scroll the page down.
        GLib.idle_add(lambda: window.set_focus(None) or False)

    def on_refresh(self, *_):
        window = self.props.active_window
        if window:
            window.reload_prefixes(select=window.prefix.path if window.prefix else None)
            window.refresh_audio()
            window.refresh_status()

    def on_about(self, *_):
        Adw.AboutDialog(
            application_name="WineASIO Settings", application_icon=APP_ID,
            version=__version__, developer_name="Knirby",
            comments="Buffer size, sample rate and driver settings for WineASIO under PipeWire.",
            website="https://github.com/knirby/wineasio-gui",
            issue_url="https://github.com/knirby/wineasio-gui/issues",
            license_type=Gtk.License.GPL_3_0_ONLY).present(self.props.active_window)


def run():
    return Application().run([])
