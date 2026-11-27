// Nerd Natter indicator for nerd-dictation (Cinnamon applet).
//
// Holds one connection to the container's control port and sends "watch". The
// server then pushes "state <idle|active> <0|1>" lines (1 = hands-free), so the
// icon changes the moment the mic really goes hot or cold. If the connection
// drops the service is down: the icon goes to "offline" and we retry.
//
// Left click toggles hands-free mode (in that mode the push-to-talk key toggles
// dictation instead of being held). Right click has the rest.

const Applet = imports.ui.applet;
const PopupMenu = imports.ui.popupMenu;
const Settings = imports.ui.settings;
const Main = imports.ui.main;
const Util = imports.misc.util;
const Mainloop = imports.mainloop;
const Gio = imports.gi.Gio;
const GLib = imports.gi.GLib;

// Control socket: a file in your runtime folder, no network port (see nn_server.py)
const SOCKET_PATH = (GLib.getenv('XDG_RUNTIME_DIR') || '/tmp') + '/nerd-natter/control.sock';
const RETRY_SECONDS = 1;   // local connection attempts are cheap; keeps the gap after a restart short
const COLOR_ACTIVE = '#e53935';
const COLOR_HANDSFREE = '#f5a623';
// typing mode shown on the icon (see _updateUi)
const MODE_TEXT = '#4a90d9';           // normal text
const MODE_COMMAND = '#3fb950';        // terminal commands
const MODE_VOICE_COMMAND = '#b57bee';  // command mode chosen by voice
const MODE_VOICE_TEXT = '#cfcfcf';     // text mode chosen by voice

class NnApplet extends Applet.IconApplet {
    constructor(metadata, orientation, panelHeight, instanceId) {
        super(orientation, panelHeight, instanceId);
        this.metadata = metadata;

        this.state = 'offline';   // offline | idle | active
        this.handsfree = false;
        this.startBeeped = false; // the start beep already played for this activation
        this.synced = false;      // false until the first state line of a connection
        this.conn = null;
        this.reader = null;
        this.gen = 0;             // invalidates callbacks of an old connection
        this.retryId = 0;
        this.destroyed = false;
        this.reminderId = 0;      // timer for the hands-free reminder beep
        this.volumeId = 0;        // debounce timer for the volume preview
        this.configId = 0;        // debounce timer for writing config.env
        this.keepAwakeProc = null;
        this.keepMicProc = null;
        this.autostartId = 0;
        this.modelLoading = false;  // the speech model is still loading (server says so)
        this.pulseId = 0;           // timer that pulses the icon while it loads
        this.typing = null;       // {mode: 'command'|'text', by: 'auto'|'voice'} for the focused window
        this.modeId = 0;          // debounce timer for looking up the typing mode
        this.focusSignal = 0;

        this.settings = new Settings.AppletSettings(this, metadata.uuid, instanceId);
        for (let [key, prop] of [['model-dir', 'modelDir'], ['nerd-dir', 'nerdDir'], ['run-mode', 'runMode'],
                                 ['nn-config', 'nnConfig'], ['nn-args', 'nnArgs'],
                                 ['live-text', 'liveText'], ['keycodes', 'keycodes'],
                                 ['beep-lead-ms', 'beepLeadMs'], ['release-delay-ms', 'releaseDelayMs']])
            this.settings.bind(key, prop, this._queueConfigWrite.bind(this));
        this.settings.bind('autostart-service', 'autostartService');
        this.settings.bind('sound-enabled', 'soundEnabled', () => { this._updateReminder(); this._updateKeepAwake(); });
        this.settings.bind('keep-awake', 'keepAwake', this._updateKeepAwake.bind(this));
        this.settings.bind('keep-mic-awake', 'keepMicAwake', this._updateKeepAwake.bind(this));
        this.settings.bind('icon-style', 'iconStyle', this._updateUi.bind(this));
        this.settings.bind('mode-indicator', 'modeIndicator', () => { this._updateUi(); this._refreshMode(); });
        this.settings.bind('project-dir', 'projectDir', this._queueConfigWrite.bind(this));
        this.settings.bind('sound-volume', 'soundVolume', this._onVolumeChanged.bind(this));
        this.settings.bind('sound-on', 'soundOn');
        this.settings.bind('sound-off', 'soundOff');
        this.settings.bind('sound-remind', 'soundRemind');
        this.settings.bind('reminder-beep', 'reminderBeep', this._onReminderChanged.bind(this));
        this.settings.bind('reminder-seconds', 'reminderSeconds', this._onReminderChanged.bind(this));

        this._buildMenu();
        this.focusSignal = global.display.connect('notify::focus-window', () => this._queueModeRefresh());
        this._updateUi();
        this._connect();
        this.syncing = false;
        this.configMonitor = null;
        this.syncId = 0;
        this._watchConfigFile();
        this._loadEffective();
        this._autostart();
        this._updateKeepAwake();
    }

    // --- menu ---------------------------------------------------------------
    _buildMenu() {
        let menu = this._applet_context_menu;

        this.hfItem = new PopupMenu.PopupSwitchMenuItem('Hands-free mode', false);
        this.hfItem.connect('toggled', (item, state) => {
            // setToggleState from a server update must not send a command back
            if (state !== this.handsfree)
                this._sendCommand('handsfree ' + (state ? 'on' : 'off'));
        });
        menu.addMenuItem(this.hfItem);

        this.remindItem = new PopupMenu.PopupSwitchMenuItem('Reminder beep while hands-free', !!this.reminderBeep);
        this.remindItem.connect('toggled', (item, state) => {
            if (state !== !!this.reminderBeep)
                this.reminderBeep = state;   // the binding saves it and calls _onReminderChanged
        });
        menu.addMenuItem(this.remindItem);

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addItem('Start service', () => this._ctl(['start']));
        this._addItem('Stop service', () => this._ctl(['stop']));
        this._addItem('Restart service', () => this._ctl(['restart']));
        this._addItem('Rebuild image and restart', () => this._ctl(['restart', '--build']));

        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._addItem('View service log', () => this._logs());
        this._addItem('View key watcher log', () => this._logs('watcher'));
        this._addItem('Help and website', () => Util.spawn(['python3', this.metadata.path + '/about.py']));
        this._addItem('Edit my own rules (kept when updating)', () => this._editRules());
        this._addItem('Edit nn-config.py', () => this._editConfig());
        // Cinnamon adds About, Configure and Remove itself, from metadata.json
    }

    _addItem(label, callback) {
        let item = new PopupMenu.PopupMenuItem(label);
        item.connect('activate', callback);
        this._applet_context_menu.addMenuItem(item);
    }

    on_applet_clicked(event) {
        if (this.state !== 'offline')
            this._sendCommand('handsfree toggle');
    }

    // --- project directory and make targets ------------------------------------
    _projectDir() {
        let dir = this._fsPath(this.projectDir) || this._recordedProjectDir();
        if (!dir)
            Main.notify('Nerd Natter', 'Project directory unknown. Run make install, or set it in the applet settings.');
        return dir;
    }

    _recordedProjectDir() {
        if (this.projectDir)
            return this._fsPath(this.projectDir);
        try {
            let path = GLib.get_user_config_dir() + '/nerd-natter/project-dir';
            let [ok, data] = GLib.file_get_contents(path);
            if (ok) {
                let text = (typeof TextDecoder !== 'undefined')
                    ? new TextDecoder().decode(data)
                    : imports.byteArray.toString(data);
                if (text.trim())
                    return text.trim();
            }
        } catch (e) { /* fall through */ }
        return null;
    }

    // --- service control: everything goes through <project>/nn-ctl ---------------------
    // Settings are saved to ~/.config/nerd-natter/config.env, which nn-ctl (and the
    // Makefile) read. Keys this applet does not manage are left alone when it rewrites it.
    _fsPath(value) {
        if (!value)
            return '';
        let path = value.replace(/^file:\/\//, '');
        try { path = decodeURIComponent(path); } catch (e) { /* keep as is */ }
        return path;
    }

    _configPath() {
        return GLib.get_user_config_dir() + '/nerd-natter/config.env';
    }

    _queueConfigWrite() {
        if (this.syncing)
            return;
        if (this.configId)
            Mainloop.source_remove(this.configId);
        this.configId = Mainloop.timeout_add(600, () => {
            this.configId = 0;
            this._writeConfig();
            return false;
        });
    }

    _writeConfig() {
        let managed = {};
        let dir = this._fsPath(this.projectDir) || this._recordedProjectDir();
        if (dir)
            managed.PROJECT_DIR = dir;
        managed.NN_LIVE = this.liveText ? '1' : '0';
        if (this.beepLeadMs !== undefined)
            managed.BEEP_LEAD = String(this.beepLeadMs / 1000);
        if (this.releaseDelayMs !== undefined)
            managed.RELEASE_DELAY = String(this.releaseDelayMs / 1000);
        let optional = {
            MODEL_DIR: this._fsPath(this.modelDir),
            NERD_DIR: this._fsPath(this.nerdDir),
            RUN_MODE: this.runMode,
            NN_CONFIG: this._fsPath(this.nnConfig),
            NN_ARGS: (this.nnArgs || '').trim(),
            NN_KEYCODES: (this.keycodes || '').replace(/\s+/g, ''),
        };
        for (let k in optional)
            if (optional[k])
                managed[k] = optional[k];

        let path = this._configPath();
        let lines = [];
        try {
            let [ok, data] = GLib.file_get_contents(path);
            if (ok)
                lines = imports.byteArray.toString(data).split('\n');
        } catch (e) { /* no file yet */ }
        let seen = {};
        let out = [];
        for (let line of lines) {
            let m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)=/);
            if (m && m[1] in managed) {
                if (!seen[m[1]])
                    out.push(m[1] + '=' + managed[m[1]]);
                seen[m[1]] = true;
            } else if (line !== '' || out.length) {
                out.push(line);
            }
        }
        while (out.length && out[out.length - 1] === '')
            out.pop();
        if (!out.length)
            out.push('# Nerd Natter settings, written by the applet Configure dialog. Plain KEY=VALUE, no quotes.');
        for (let k in managed)
            if (!seen[k])
                out.push(k + '=' + managed[k]);
        try {
            GLib.mkdir_with_parents(GLib.path_get_dirname(path), 0o755);
            GLib.file_set_contents(path, out.join('\n') + '\n');
        } catch (e) {
            global.logError('nn applet: cannot write ' + path + ': ' + e);
        }
    }

    // Run nn-ctl and tell the user if it fails (missing model, docker down, ...)
    _ctl(args, quiet, done) {
        let dir = this._projectDir();
        if (!dir)
            return;
        this._writeConfig();
        try {
            let proc = new Gio.Subprocess({
                argv: ['bash', dir + '/nn-ctl', ...args],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                let out = '';
                try { [, out] = p.communicate_utf8_finish(res); } catch (e) { out = String(e); }
                if (p.get_successful() && (args[0] === 'start' || args[0] === 'restart'))
                    this._reconnectNow();
                if (done)
                    done(p.get_successful());
                if (!p.get_successful() && !quiet) {
                    let tail = (out || '').trim().split('\n').slice(-4).join('\n');
                    Main.notifyError('Nerd Natter: ' + args[0] + ' failed', tail || 'no output');
                }
            });
        } catch (e) {
            Main.notifyError('Nerd Natter', 'Cannot run nn-ctl in ' + dir + ': ' + e);
            if (done)
                done(false);
        }
    }

    // Configure dialog button: press the key you want and its X11 keycode fills the field.
    // A small helper (nn-ctl capture-key) takes the whole keyboard for a moment, so it
    // sees the key whatever window has focus and even if push-to-talk already holds it.
    _captureKey() {
        if (this.capturing)
            return;
        let dir = this._projectDir();
        if (!dir)
            return;
        this.capturing = true;
        Main.notify('Nerd Natter', 'Press the key you want to talk with now. Escape cancels.');
        try {
            let proc = new Gio.Subprocess({
                argv: ['bash', dir + '/nn-ctl', 'capture-key'],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                this.capturing = false;
                let out = '', err = '';
                try { [, out, err] = p.communicate_utf8_finish(res); } catch (e) { err = String(e); }
                let code = parseInt((out || '').trim());
                if (p.get_exit_status() === 0 && code > 0) {
                    this.keycodes = String(code);   // saved: the Configure field updates by itself
                    this._writeConfig();
                    if (this.state !== 'offline')
                        this._ctl(['watcher'], true);   // only the key watcher needs the new key
                    Main.notify('Nerd Natter', 'Key code ' + code + ' chosen and in use.');
                } else if (p.get_exit_status() === 2) {
                    Main.notifyError('Nerd Natter', (err || '').trim() || 'Could not read the keyboard.');
                } else {
                    Main.notify('Nerd Natter', 'No key chosen.');
                }
            });
        } catch (e) {
            this.capturing = false;
            Main.notifyError('Nerd Natter', 'Cannot run nn-ctl in ' + dir + ': ' + e);
        }
    }

    // --- keeping Configure and config.env in step ---------------------------------------
    // The Configure fields show what is really in use (the defaults when nothing is set), and
    // follow changes made elsewhere, for example by hand or by make.
    _watchConfigFile() {
        let path = this._configPath();
        try {
            GLib.mkdir_with_parents(GLib.path_get_dirname(path), 0o755);
            this.configMonitor = Gio.File.new_for_path(path).monitor_file(Gio.FileMonitorFlags.NONE, null);
            this.configMonitor.connect('changed', () => {
                if (this.syncId)
                    Mainloop.source_remove(this.syncId);
                this.syncId = Mainloop.timeout_add(400, () => {
                    this.syncId = 0;
                    this._loadEffective();
                    return false;
                });
            });
        } catch (e) {
            global.logError('nn applet: cannot watch ' + path + ': ' + e);
        }
    }

    _loadEffective() {
        let dir = this._fsPath(this.projectDir) || this._recordedProjectDir();
        if (!dir)
            return;
        try {
            let proc = new Gio.Subprocess({
                argv: ['bash', dir + '/nn-ctl', 'config'],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                let out = '';
                try { [, out] = p.communicate_utf8_finish(res); } catch (e) { return; }
                let cfg = {};
                for (let line of (out || '').split('\n')) {
                    let m = line.match(/^([A-Z_]+)=(.*)$/);
                    if (m)
                        cfg[m[1]] = m[2];
                }
                if (cfg.PROJECT_DIR)
                    this._applyEffective(cfg);
            });
        } catch (e) { /* nn-ctl not there yet: the fields stay as they are */ }
    }

    _applyEffective(cfg) {
        this.syncing = true;
        let put = (prop, value) => {
            if (value !== undefined && this[prop] !== value)
                this[prop] = value;
        };
        put('projectDir', cfg.PROJECT_DIR);
        put('nerdDir', cfg.NERD_DIR);
        put('modelDir', cfg.MODEL_DIR);
        put('nnConfig', cfg.NN_CONFIG);
        put('runMode', cfg.RUN_MODE);
        put('nnArgs', cfg.NN_ARGS);
        put('keycodes', cfg.NN_KEYCODES);
        put('liveText', cfg.NN_LIVE === '1');
        if (cfg.BEEP_LEAD && !isNaN(parseFloat(cfg.BEEP_LEAD)))
            put('beepLeadMs', Math.round(parseFloat(cfg.BEEP_LEAD) * 1000));
        if (cfg.RELEASE_DELAY && !isNaN(parseFloat(cfg.RELEASE_DELAY)))
            put('releaseDelayMs', Math.round(parseFloat(cfg.RELEASE_DELAY) * 1000));
        this.syncing = false;
    }

    // Configure dialog button
    _applyRestart() {
        this._ctl(['restart']);
    }

    // Logs open in a terminal window that stays open after the command ends, so an
    // error such as "container is not running" can still be read.
    _logs(which) {
        let dir = this._projectDir();
        if (!dir)
            return;
        let script = 'bash "$1" logs ' + (which || '') + '; echo; read -n 1 -s -r -p "Press any key to close"';
        let inner = ['bash', '-c', script, 'nn-logs', dir + '/nn-ctl'];
        if (GLib.find_program_in_path('gnome-terminal'))
            Util.spawn(['gnome-terminal', '--title=Nerd Natter log', '--', ...inner]);
        else if (GLib.find_program_in_path('x-terminal-emulator'))
            Util.spawn(['x-terminal-emulator', '-e', ...inner]);
        else
            Main.notifyError('Nerd Natter', 'No terminal found. Run in a terminal: ' + dir + '/nn-ctl logs');
    }

    // Opens ~/.config/nerd-natter/user-rules.py, creating it with examples the first time
    _editRules() {
        let dir = this._projectDir();
        if (!dir)
            return;
        try {
            let proc = new Gio.Subprocess({
                argv: ['bash', dir + '/nn-ctl', 'rules'],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                let out = '';
                try { [, out] = p.communicate_utf8_finish(res); } catch (e) { out = ''; }
                let path = (out || '').trim();
                if (p.get_successful() && path)
                    Util.spawn(['xdg-open', path]);
                else
                    Main.notifyError('Nerd Natter', 'Could not open your rules file.');
            });
        } catch (e) {
            Main.notifyError('Nerd Natter', 'Cannot run nn-ctl in ' + dir + ': ' + e);
        }
    }

    _editConfig() {
        let dir = this._projectDir();
        if (dir)
            Util.spawn(['xdg-open', this._fsPath(this.nnConfig) || dir + '/nn-config.py']);
    }

    // With "start when the applet loads" on: if the service is not answering a few seconds
    // after login, start it.
    _autostart() {
        if (!this.autostartService)
            return;
        this.autostartId = Mainloop.timeout_add_seconds(6, () => {
            this.autostartId = 0;
            if (this.state === 'offline' && !this.destroyed)
                this._ctl(['start']);
            return false;
        });
    }

    // --- connection to the container ------------------------------------------
    _connect() {
        if (this.destroyed)
            return;
        let gen = ++this.gen;
        // No client.timeout here: GIO applies it to reads too, and this connection is
        // legitimately silent between the server's keepalive pings.
        let client = new Gio.SocketClient();
        client.connect_async(new Gio.UnixSocketAddress({ path: SOCKET_PATH }), null, (c, res) => {
            if (gen !== this.gen)
                return;
            let conn;
            try {
                conn = c.connect_finish(res);
                conn.get_output_stream().write_all('watch\n', null);
            } catch (e) {
                this._lost(gen);
                return;
            }
            this.conn = conn;
            this.reader = new Gio.DataInputStream({ base_stream: conn.get_input_stream() });
            this._readLine(gen);
        });
    }

    _readLine(gen) {
        this.reader.read_line_async(GLib.PRIORITY_DEFAULT, null, (reader, res) => {
            if (gen !== this.gen)
                return;
            let line = null;
            try {
                [line] = reader.read_line_finish_utf8(res);
            } catch (e) { /* treated like EOF */ }
            if (line === null) {
                this._lost(gen);
                return;
            }
            this._onLine(line);
            this._readLine(gen);
        });
    }

    _lost(gen) {
        if (gen !== this.gen)
            return;
        this._closeConn();
        this.synced = false;
        this.handsfree = false;
        this.modelLoading = false;
        this._setState('offline');
        if (this.destroyed || this.retryId)
            return;
        this.retryId = Mainloop.timeout_add_seconds(RETRY_SECONDS, () => {
            this.retryId = 0;
            this._connect();
            return false;
        });
    }

    _closeConn() {
        if (this.conn) {
            try { this.conn.close(null); } catch (e) { /* already closed */ }
            this.conn = null;
        }
        this.reader = null;
    }

    // Drop the current connection and try again at once (after the service was restarted)
    _reconnectNow() {
        this._resetConnection();
    }

    _resetConnection() {
        this.gen++;   // old callbacks become no-ops
        this._closeConn();
        if (this.retryId) {
            Mainloop.source_remove(this.retryId);
            this.retryId = 0;
        }
        this.synced = false;
        this._connect();
    }

    _sendCommand(cmd) {
        let client = new Gio.SocketClient();
        client.timeout = 3;
        client.connect_async(new Gio.UnixSocketAddress({ path: SOCKET_PATH }), null, (c, res) => {
            try {
                let conn = c.connect_finish(res);
                conn.get_output_stream().write_all(cmd + '\n', null);
                conn.close(null);
            } catch (e) {
                global.logError('nn applet: command "' + cmd + '" failed: ' + e);
            }
        });
    }

    // --- state ------------------------------------------------------------------
    _onLine(line) {
        let parts = line.trim().split(/\s+/);
        if (parts[0] === 'model') {
            this.modelLoading = parts[1] === 'loading';
            this._updateUi();
            return;
        }
        if (parts[0] === 'loading') {
            Main.notify('Nerd Natter', 'The speech model is still loading. Try again in a few seconds.');
            return;
        }
        if (parts[0] === 'starting') {
            // the server holds the mic closed for a moment so this beep is not recorded
            if (this.synced && this.soundEnabled) {
                this.startBeeped = true;
                this._beep('on');
            }
            return;
        }
        if (parts[0] !== 'state')
            return;   // "ping" keepalive
        let beepAllowed = this.synced;
        this.synced = true;
        this.handsfree = parts[2] === '1';
        this._setState(parts[1] === 'active' ? 'active' : 'idle', beepAllowed);
    }

    _setState(state, beepAllowed) {
        let prev = this.state;
        this.state = state;
        if (beepAllowed && this.soundEnabled) {
            if (prev !== 'active' && state === 'active') {
                if (!this.startBeeped)   // older server without the "starting" line
                    this._beep('on');
            }
            else if (prev === 'active' && state === 'idle')
                this._beep('off');
        }
        this.startBeeped = false;
        this._updateUi();
        this._updateReminder();
        // a voice switch ("command mode") is spoken while dictating, so look again when it ends
        if (state === 'idle' || prev === 'offline')
            this._queueModeRefresh();
    }

    // Gentle fade in and out while the model loads, so it looks busy and not broken
    _pulse(on) {
        if (!on) {
            if (this.pulseId) {
                Mainloop.source_remove(this.pulseId);
                this.pulseId = 0;
            }
            this.actor.opacity = 255;
            return;
        }
        if (this.pulseId)
            return;
        let dim = false;
        this.pulseId = Mainloop.timeout_add(600, () => {
            dim = !dim;
            this.actor.opacity = dim ? 110 : 255;
            return true;
        });
    }

    // --- typing mode (normal text or terminal commands) -------------------------------
    // The rules live in nn-config.py, so ask it: it knows the terminal list, your own rules
    // and whether you chose a mode by voice. We pass it the focused window's class.
    _queueModeRefresh() {
        if (this.modeId)
            Mainloop.source_remove(this.modeId);
        this.modeId = Mainloop.timeout_add(150, () => { this.modeId = 0; this._refreshMode(); return false; });
    }

    _refreshMode() {
        if (this.destroyed)
            return;
        if (this.modeIndicator === 'off' || this.state === 'offline') {
            this.typing = null;
            this._updateUi();
            return;
        }
        let dir = this._projectDir();
        let rules = this._fsPath(this.nnConfig) || (dir ? dir + '/nn-config.py' : '');
        if (!rules)
            return;
        let win = global.display.focus_window;
        let cls = win ? (win.get_wm_class() || '') : '';
        try {
            let proc = new Gio.Subprocess({
                argv: ['python3', rules, '--mode', cls.toLowerCase()],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                let out = '';
                try { [, out] = p.communicate_utf8_finish(res); } catch (e) { out = ''; }
                let [mode, by] = (out || '').trim().split(' ');
                this.typing = (p.get_successful() && mode) ? { mode: mode, by: by || 'auto' } : null;
                if (!this.typing)
                    global.logError('Nerd Natter: could not read the typing mode from ' + rules);
                if (!this.destroyed)
                    this._updateUi();
            });
        } catch (e) {
            this.typing = null;
        }
    }

    // While dictating hands-free there is no held key to remind you the mic is hot, so
    // optionally play a soft tick every N seconds (default 15). Not in PTT mode.
    _onReminderChanged() {
        this.remindItem.setToggleState(!!this.reminderBeep);
        this._updateReminder();
    }

    _updateReminder() {
        let want = !this.destroyed && this.state === 'active' && this.handsfree &&
                   this.reminderBeep && this.soundEnabled;
        if (want && !this.reminderId) {
            let secs = Math.max(5, this.reminderSeconds || 15);
            this.reminderId = Mainloop.timeout_add_seconds(secs, () => {
                this._beep('remind');
                return true;
            });
        } else if (!want && this.reminderId) {
            Mainloop.source_remove(this.reminderId);
            this.reminderId = 0;
        }
    }

    // A user chosen file (setting sound-on / sound-off / sound-remind) wins; blank or
    // missing falls back to the bundled sound. Settings may hold a path or a file:// URI.
    _soundFile(name) {
        let custom = name === 'on' ? this.soundOn : name === 'off' ? this.soundOff : this.soundRemind;
        if (custom) {
            let path = this._fsPath(custom);
            if (GLib.file_test(path, GLib.FileTest.EXISTS))
                return path;
        }
        return this.metadata.path + '/sounds/' + name + '.wav';
    }

    // The slider is a loudness target (% of full output), independent of the system volume:
    // the stream volume is the target divided by the current default sink volume, so
    // turning the system volume up or down does not change how loud these sounds are.
    // Capped at 200% so a very low system volume cannot cause heavy clipping.
    _sinkVolume(callback) {
        try {
            let proc = new Gio.Subprocess({
                argv: ['pactl', 'get-sink-volume', '@DEFAULT_SINK@'],
                flags: Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            });
            proc.init(null);
            proc.communicate_utf8_async(null, null, (p, res) => {
                let pct = null;
                try {
                    let [ok, out] = p.communicate_utf8_finish(res);
                    let m = out && out.match(/(\d+)%/);
                    if (m)
                        pct = parseInt(m[1]);
                } catch (e) { /* fall back to a plain stream volume */ }
                callback(pct);
            });
        } catch (e) {
            callback(null);
        }
    }

    // A sound card that has been idle for a few seconds goes to sleep, and the first sound
    // after that is cut off or swallowed while it wakes up (and the wake-up can click, which
    // the microphone hears as a stray word). A Bluetooth headset also needs a moment to
    // reopen its microphone, which loses the first words you say. Endless silence played
    // (and, optionally, recorded and thrown away) keeps them awake.
    _keepStream(prop, argv) {
        try {
            let proc = new Gio.Subprocess({
                argv: argv,
                flags: Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
            });
            proc.init(null);
            this[prop] = proc;
            proc.wait_async(null, () => { if (this[prop] === proc) this[prop] = null; });
        } catch (e) {
            global.logError('nn applet: cannot start ' + argv[0] + ': ' + e);
        }
    }

    _stopStream(prop) {
        let proc = this[prop];
        this[prop] = null;
        if (proc) {
            try { proc.force_exit(); } catch (e) { /* already gone */ }
        }
    }

    _updateKeepAwake() {
        let wantOut = !this.destroyed && this.keepAwake && this.soundEnabled;
        let wantMic = !this.destroyed && this.keepAwake && this.keepMicAwake;
        if (wantOut && !this.keepAwakeProc)
            this._keepStream('keepAwakeProc', ['pacat', '--playback', '--rate=8000', '--format=s16le',
                '--channels=1', '--latency-msec=200', '--client-name=Nerd Natter',
                '--stream-name=keep sound device awake', '/dev/zero']);
        else if (!wantOut)
            this._stopStream('keepAwakeProc');
        if (wantMic && !this.keepMicProc)
            this._keepStream('keepMicProc', ['parec', '--rate=8000', '--format=s16le', '--channels=1',
                '--latency-msec=200', '--client-name=Nerd Natter',
                '--stream-name=keep microphone awake']);
        else if (!wantMic)
            this._stopStream('keepMicProc');
    }

    _beep(name) {
        let file = this._soundFile(name);
        let target = Math.max(0, Math.min(100, this.soundVolume === undefined ? 50 : this.soundVolume)) / 100;
        this._sinkVolume(sinkPct => {
            let sink = sinkPct === null ? 1 : Math.max(sinkPct, 5) / 100;
            let stream = Math.min(2.0, target / sink);
            Util.spawn(['paplay', '--volume=' + Math.round(65536 * stream), file]);
        });
    }

    // Preview while the slider moves: play once it has rested for a moment (mouse up).
    _onVolumeChanged() {
        if (this.volumeId)
            Mainloop.source_remove(this.volumeId);
        this.volumeId = Mainloop.timeout_add(400, () => {
            this.volumeId = 0;
            this._beep('on');
            return false;
        });
    }

    _testSounds() {
        this._beep('on');
        Mainloop.timeout_add(900, () => { this._beep('off'); return false; });
    }

    _updateUi() {
        let hf = this.handsfree;
        let key, color, tip;   // key names the icon files: logo-<key>.png, nn-<key>-symbolic.svg
        if (this.state === 'offline') {
            key = 'offline'; color = '';
            tip = 'Nerd Natter: service not running';
        } else if (this.modelLoading && this.state !== 'active') {
            key = 'loading'; color = '';
            tip = 'Nerd Natter: loading the speech model, one moment...';
        } else if (this.state === 'active') {
            key = hf ? 'active-hf' : 'active'; color = COLOR_ACTIVE;
            tip = hf ? 'Nerd Natter: dictating hands-free, press the key to stop'
                     : 'Nerd Natter: dictating, mic is hot';
        } else {
            key = hf ? 'idle-hf' : 'idle'; color = hf ? COLOR_HANDSFREE : '';
            tip = hf ? 'Nerd Natter: hands-free mode, press the key to start'
                     : 'Nerd Natter: idle, hold the key to talk';
        }
        if (this.iconStyle === 'symbolic') {
            this.set_applet_icon_symbolic_path(this.metadata.path + '/icons/nn-' + key + '-symbolic.svg');
            this._applet_icon.set_style(color ? 'color: ' + color + ';' : '');
        } else {
            // the logo: grey when idle, colour while dictating, dot for hands-free
            this.set_applet_icon_path(this.metadata.path + '/icons/logo-' + key + '.png');
        }
        // typing mode: a coloured line or glow, so you know whether "slash" will type /
        let shade = '';
        if (this.typing && this.state !== 'offline' && this.modeIndicator !== 'off') {
            let c = this.typing.by === 'voice'
                ? (this.typing.mode === 'command' ? MODE_VOICE_COMMAND : MODE_VOICE_TEXT)
                : (this.typing.mode === 'command' ? MODE_COMMAND : MODE_TEXT);
            // plain borders and background colours are drawn by every theme (box-shadow is not)
            shade = this.modeIndicator === 'glow'
                ? 'background-color: ' + c + '55; border: 2px solid ' + c + '; border-radius: 8px;'
                : 'border-top: 3px solid ' + c + ';';
            tip += '\nTyping: ' + (this.typing.mode === 'command' ? 'terminal commands' : 'normal text') +
                   (this.typing.by === 'voice' ? ' (you chose this by voice; say "automatic mode" to undo)' : '');
        }
        this.actor.set_style(shade);
        this._pulse(key === 'loading');
        this.set_applet_tooltip(tip);
        this.hfItem.setToggleState(hf);
    }

    on_applet_removed_from_panel() {
        this.destroyed = true;
        this.gen++;
        if (this.retryId) {
            Mainloop.source_remove(this.retryId);
            this.retryId = 0;
        }
        this._updateReminder();   // destroyed is set: stops the timer
        this._updateKeepAwake();   // destroyed is set: stops the silent stream
        if (this.configMonitor) {
            this.configMonitor.cancel();
            this.configMonitor = null;
        }
        if (this.focusSignal) {
            global.display.disconnect(this.focusSignal);
            this.focusSignal = 0;
        }
        for (let id of ['volumeId', 'configId', 'autostartId', 'syncId', 'modeId', 'pulseId']) {
            if (this[id]) {
                Mainloop.source_remove(this[id]);
                this[id] = 0;
            }
        }
        this._closeConn();
        this.settings.finalize();
    }
}

function main(metadata, orientation, panelHeight, instanceId) {
    return new NnApplet(metadata, orientation, panelHeight, instanceId);
}
