// Mus popup: a little mouse that scurries in from a screen edge, takes one prompt, and talks to `mus serve`.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';
import Cairo from 'cairo';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const W = 340;        // base mouse width, tail included
const W_TALL = 262;   // vertical edges: narrow and tall
const W_WIDE = 388;   // horizontal edges: wide and short
const W_SQUARE = 316; // corners: compact
const TAIL = 44;      // tail box width
const EAR = 46;
const PAD = 10;       // gap to the screen edge
const AUTO_HIDE_MS = 2500;
const SHOW_MS = 1200;  // a reason face stays at least this long, so a quick retry is still seen
const DOWN = 'Mus isn\'t running. Start it with: systemctl --user start mus';
const OFFLINE = 'You\'re offline. Remembered answers still work.';
const SOCK = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'mus.sock']);

// same tokens as stylesheet.css, for cairo
const FUR_DARK = [0x4e / 255, 0x58 / 255, 0x64 / 255];
const FUR = [0x7f / 255, 0x8b / 255, 0x99 / 255];
const PINK = [0xe8 / 255, 0xa3 / 255, 0xa8 / 255];
const INK = [0x1f / 255, 0x25 / 255, 0x2b / 255];
const SCAR = [0.80, 0.34, 0.37];

// How Mus looks while it waits. A big errand is just a big errand: time alone walks it through WORK.
// When the daemon says why it's slow, that reason gets its own face, so the face tells you what's holding it up.
const WORK = [  // [after ms, mood, trait, tail swing ms (0 = still)]
    [0, 'think', 'none', 280],
    [3000, 'focus', 'none', 220],
    [7000, 'focus', 'sweat', 170],
    [14000, 'strain', 'sweat', 120],
    [25000, 'weary', 'sweat', 700],
];
const WAIT = {  // why: [mood, trait, tail swing ms]
    model: ['bored', 'dots', 900],          // network is fine, Gemini is slow to start
    network: ['worried', 'signal', 200],    // Google is slow to reach
    offline: ['worried', 'nosignal', 200],  // Google can't be reached
    quota: ['dizzy', 'none', 0],            // a key ran out, switching to the next
    retry: ['grit', 'none', 180],           // a script failed, trying another way
};
const FAIL = {  // error why: [mood, trait]; anything else is sad with a scar
    network: ['worried', 'nosignal'],
    quota: ['dizzy', 'sweat'],    // went round every key
    key: ['confused', 'question'],
    blocked: ['wide', 'blush'],   // the model said no
    down: ['sleepy', 'zzz'],
};
const BROWS = {  // [inner, outer] brow height over the eye line, left eye then right
    worried: [[-10, -6], [-10, -6]],   // inner ends up
    grit: [[-6, -10], [-6, -10]],      // inner ends down: determined
    confused: [[-7, -7], [-13, -8]],   // one brow arched up
};
const SHUT = ['sleepy', 'strain', 'weary', 'dizzy'];  // eyes with no blink

function markup(text) {
    return GLib.markup_escape_text(text, -1)
        .replace(/`([^`\n]+)`/g, '<tt>$1</tt>')
        .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
}

class Mouse {
    constructor() {
        this._mood = 'idle';
        this._lookX = 0;
        this._blinking = false;
        this._trait = 'none';        // cosmetic overlay: blush / sweat / scar / bolt / ...
        this._details = '';          // what went wrong, in words; Ctrl+D shows it
        this._gotFirst = false;
        this._t0 = 0;
        this._ttftMs = 0;
        this._run = null;
        this._answer = '';
        this._timers = new Set();

        this.actor = new St.Widget({reactive: true, track_hover: true, visible: false});

        this._tail = new St.DrawingArea({width: TAIL, height: 56});
        this._tail.connect('repaint', a => this._drawTail(a));
        this._ears = [0, 1].map(() => {
            const ear = new St.Widget({style_class: 'mus-ear', width: EAR, height: EAR});
            ear.add_child(new St.Widget({style_class: 'mus-ear-inner', x: 11, y: 11, width: 24, height: 24}));
            return ear;
        });

        this._face = new St.BoxLayout({style_class: 'mus-face', vertical: true, y: 24, width: W - TAIL + 14});
        this._mug = new St.DrawingArea({height: 34, x_expand: true});
        this._mug.connect('repaint', a => this._drawMug(a));
        this._entry = new St.Entry({style_class: 'mus-entry', hint_text: 'What\'s the errand?', can_focus: true, x_expand: true});
        this._entry.clutter_text.connect('activate', () => (this._pending ? this._reply(true) : this._submit(false)));
        this._status = new St.Label({style_class: 'mus-status', visible: false});
        this._status.clutter_text.line_wrap = true;

        this._approve = new St.BoxLayout({style_class: 'mus-approve', vertical: true, visible: false});
        this._approveCmd = new St.Label({style_class: 'mus-approve-cmd'});
        this._approveCmd.clutter_text.line_wrap = true;
        const row = new St.BoxLayout({style_class: 'mus-approve-row', x_align: Clutter.ActorAlign.END});
        const allow = new St.Button({style_class: 'mus-btn mus-allow', label: 'Allow', can_focus: true});
        const deny = new St.Button({style_class: 'mus-btn mus-deny', label: 'Deny', can_focus: true});
        allow.connect('clicked', () => this._reply(true));
        deny.connect('clicked', () => this._reply(false));
        row.add_child(deny);
        row.add_child(allow);
        const title = new St.Label({style_class: 'mus-approve-title', text: 'Needs root or can\'t be undone. Allow for this errand?'});
        title.clutter_text.line_wrap = true;
        this._approve.add_child(title);
        this._approve.add_child(this._approveCmd);
        this._approve.add_child(row);

        this._scroll = new St.ScrollView({style_class: 'mus-answer-box', visible: false,
            hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC});
        const box = new St.BoxLayout({vertical: true});
        this._label = new St.Label({style_class: 'mus-answer', reactive: true});
        this._label.clutter_text.line_wrap = true;
        this._label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        this._label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        this._label.connect('button-press-event', () => this._copy());
        box.add_child(this._label);
        this._scroll.set_child(box);

        for (const w of [this._mug, this._entry, this._status, this._approve, this._scroll])
            this._face.add_child(w);
        for (const w of [this._tail, ...this._ears, this._face])
            this.actor.add_child(w);

        this.actor.connect('key-press-event', (a, ev) => this._onKey(ev));
        this.actor.connect('button-press-event', (a, ev) => this._onClickOutside(ev));
        this._face.connect('notify::height', () => this._layoutTail());
        this.actor.connect('notify::height', () => this._clamp());
        this._client = new Gio.SocketClient();
        this._net = Gio.NetworkMonitor.get_default();
        this._netId = this._net.connect('network-changed', () => this._checkNet());
    }

    // --- showing and hiding -------------------------------------------------

    toggle() {
        if (this.actor.visible && !this._leaving)
            this.hide();
        else
            this.show();
    }

    show() {
        if (this.actor.visible && !this._leaving)
            return;
        this.actor.remove_all_transitions();  // reopened mid-exit: stop the exit (its onStopped hides) and come back
        if (!this._run && !this._pending) {
            this._entry.text = '';
            this._setAnswer('');
            this._setStatus('');
            this._details = '';
            this._greet();
        }
        this._place();
        this.actor.show();
        this._checkNet();
        this._scheduleBlink();
        this._grab = Main.pushModal(this.actor, {actionMode: Shell.ActionMode.POPUP});
        this._entry.grab_key_focus();
        this.actor.ease({translation_x: 0, translation_y: 0, duration: 220, mode: Clutter.AnimationMode.EASE_OUT_BACK});
        this._send({warm: true}, () => {}, () => {});
    }

    hide() {
        this._clearTimer('autohide');
        this._clearTimer('blink');
        this._clearTimer('blinkoff');
        this._clearTimer('relax');
        this._blinking = false;
        if (this._grab) {
            Main.popModal(this._grab);
            this._grab = null;
        }
        this._leaving = true;
        this.actor.ease({...this._offscreen(), duration: 150, mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onStopped: () => {
                this._leaving = false;
                this.actor.hide();
            }});
    }

    // pick a random edge, and let the aspect follow it: tall on the sides, wide on top/bottom, compact in corners
    _place() {
        const mon = Main.layoutManager.currentMonitor;
        const wa = Main.layoutManager.getWorkAreaForMonitor(mon.index);
        const edge = ['left', 'right', 'top', 'bottom'][Math.floor(Math.random() * 4)];
        const vert = edge === 'left' || edge === 'right';
        const corner = Math.random() < 0.3;   // sometimes hug a corner instead of sliding along the edge
        const [w, cls] = corner ? [W_SQUARE, 'mus-square']
            : vert ? [W_TALL, 'mus-tall'] : [W_WIDE, 'mus-wide'];
        this._w = w;
        this._face.width = w - TAIL + 14;
        this._setAspect(cls);
        const tailLeft = edge === 'left' || (edge !== 'right' && Math.random() < 0.5);
        this._layout(tailLeft);
        const [, h] = this.actor.get_preferred_height(w);
        const far = Math.random() < 0.5;       // which corner, when hugging one
        const slide = () => 0.15 + Math.random() * 0.7;  // keep off the extreme ends
        let x, y;
        if (vert) {
            x = edge === 'left' ? wa.x + PAD : wa.x + wa.width - w - PAD;
            y = corner ? (far ? wa.y + PAD : wa.y + wa.height - h - PAD)
                : wa.y + slide() * (wa.height - h);
        } else {
            y = edge === 'top' ? wa.y + PAD : wa.y + wa.height - h - PAD;
            x = corner ? (far ? wa.x + PAD : wa.x + wa.width - w - PAD)
                : wa.x + slide() * (wa.width - w);
        }
        this._edge = {edge, mon, wa};
        this.actor.set_position(Math.round(x), Math.round(y));
        const off = this._offscreen();
        this.actor.translation_x = off.translation_x;
        this.actor.translation_y = off.translation_y;
    }

    _setAspect(cls) {
        for (const c of ['mus-tall', 'mus-wide', 'mus-square'])
            this._face.remove_style_class_name(c);
        this._face.add_style_class_name(cls);
    }

    _offscreen() {
        const {edge, mon} = this._edge;
        const [x, y] = this.actor.get_position();
        const [w, h] = this.actor.get_size();
        return {
            translation_x: edge === 'left' ? mon.x - x - w : edge === 'right' ? mon.x + mon.width - x : 0,
            translation_y: edge === 'top' ? mon.y - y - h : edge === 'bottom' ? mon.y + mon.height - y : 0,
        };
    }

    // the face grows as the answer streams in: keep it on screen, and glued to the bottom edge if it came from there
    _clamp() {
        if (!this._edge || !this.actor.visible)
            return;
        const {edge, wa} = this._edge;
        const h = this.actor.height;
        let [x, y] = this.actor.get_position();
        if (edge === 'bottom' || y + h > wa.y + wa.height - PAD)
            y = Math.max(wa.y + PAD, wa.y + wa.height - h - PAD);
        this.actor.set_position(x, y);
    }

    _layout(tailLeft) {
        this._tailLeft = tailLeft;
        const faceX = tailLeft ? TAIL - 14 : 0;
        this._face.x = faceX;
        this._ears[0].set_position(faceX + 8, 0);
        this._ears[1].set_position(faceX + this._face.width - EAR - 8, 0);
        this._tail.x = tailLeft ? 0 : faceX + this._face.width - 14;
        this._tail.queue_repaint();
        this._layoutTail();
    }

    _layoutTail() {
        this._tail.y = this._face.y + this._face.height - 46;
        this._tail.set_pivot_point(this._tailLeft ? 1 : 0, 0.8);
    }

    // --- drawing ------------------------------------------------------------

    _drawTail(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        if (this._tailLeft) {  // drawn for a tail curling right, mirrored when it sits on the left
            cr.translate(w, 0);
            cr.scale(-1, 1);
        }
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setSourceRGB(...PINK);
        cr.setLineWidth(3.5);
        cr.moveTo(2, h - 14);
        cr.curveTo(w * 0.55, h - 2, w * 0.95, h - 18, w * 0.7, h * 0.35);
        cr.curveTo(w * 0.55, h * 0.1, w * 0.3, h * 0.25, w * 0.45, h * 0.4);
        cr.stroke();
        cr.$dispose();
    }

    _drawMug(area) {
        const cr = area.get_context();
        const [w] = area.get_surface_size();
        const cx = w / 2, ey = 13, dx = 30;
        cr.setLineCap(Cairo.LineCap.ROUND);

        // whiskers
        cr.setSourceRGBA(...FUR_DARK, 0.9);
        cr.setLineWidth(1.2);
        for (const s of [-1, 1]) {
            for (const [dy, len] of [[-3, 52], [1, 56], [5, 50]]) {
                cr.moveTo(cx + s * 10, 23 + dy / 2);
                cr.lineTo(cx + s * len, 23 + dy * 1.6);
            }
        }
        cr.stroke();

        // eyes
        const m = this._mood;
        cr.setSourceRGB(...INK);
        for (const s of [-1, 1]) {
            const x = cx + s * dx;
            if (this._blinking || m === 'sleepy') {       // eyes shut: a content little curve
                cr.setSourceRGB(...INK);
                cr.setLineWidth(2.2);
                cr.arc(x, ey - 2, 6, Math.PI * 0.2, Math.PI * 0.8);
                cr.stroke();
            } else if (this._mood === 'happy') {
                cr.setLineWidth(2.2);
                cr.arc(x, ey + 3, 5, Math.PI * 1.1, Math.PI * 1.9);
                cr.stroke();
            } else if (this._mood === 'sad') {
                cr.setLineWidth(2.2);
                cr.moveTo(x - 4, ey + 1);
                cr.lineTo(x + 4, ey + 1);
                cr.stroke();
            } else if (m === 'strain') {   // squeezed shut: > <
                cr.setLineWidth(2.2);
                cr.moveTo(x + s * 4, ey - 4);
                cr.lineTo(x - s * 3, ey);
                cr.lineTo(x + s * 4, ey + 4);
                cr.stroke();
            } else if (m === 'weary') {    // heavy lids: half an eye under a flat line
                cr.arc(x, ey, 5, 0, Math.PI);
                cr.fill();
                cr.setLineWidth(2.2);
                cr.moveTo(x - 6, ey);
                cr.lineTo(x + 6, ey);
                cr.stroke();
            } else if (m === 'dizzy') {    // swirls
                cr.setLineWidth(1.5);
                for (let a = 0; a < 4 * Math.PI; a += 0.3)
                    cr.lineTo(x + (0.6 + a * 0.42) * Math.cos(s * a), ey + (0.6 + a * 0.42) * Math.sin(s * a));
                cr.stroke();
            } else {
                const r = m === 'wide' ? 6.5 : 5;
                const look = m === 'think' ? 2 : 0;
                const lx = this._lookX * 2.2;                  // curious: glance to one side
                const ly = m === 'bored' ? -2.5 : 0;           // bored: eyes up, waiting on someone else
                const sq = m === 'focus' || m === 'grit' ? 0.55 : 1;  // concentrating: a squint
                cr.setSourceRGB(...INK);
                cr.save();
                cr.translate(x + lx, ey + ly);
                cr.scale(1, sq);
                cr.arc(0, 0, r, 0, 2 * Math.PI);
                cr.restore();
                cr.fill();
                cr.setSourceRGBA(1, 1, 1, 0.9);
                cr.arc(x + lx + look + 1.6, ey + ly - 1.8 * sq - look / 2, 1.5, 0, 2 * Math.PI);
                cr.fill();
                cr.setSourceRGB(...INK);
            }
        }
        if (BROWS[m]) {
            cr.setSourceRGB(...INK);
            cr.setLineWidth(1.8);
            for (const [i, s] of [-1, 1].entries()) {
                const [inner, outer] = BROWS[m][i];
                cr.moveTo(cx + s * (dx - 5), ey + inner);
                cr.lineTo(cx + s * (dx + 6), ey + outer);
            }
            cr.stroke();
        }

        // nose and the little mouth under it
        cr.setSourceRGB(...PINK);
        cr.save();
        cr.translate(cx, 22);
        cr.scale(1.3, 1);
        cr.arc(0, 0, 4, 0, 2 * Math.PI);
        cr.restore();
        cr.fill();
        cr.setSourceRGB(...FUR_DARK);
        cr.setLineWidth(1.4);
        cr.arc(cx - 3, 27, 3, 0, Math.PI);
        cr.stroke();
        cr.arc(cx + 3, 27, 3, 0, Math.PI);
        cr.stroke();

        this._drawTrait(cr, cx);
        cr.$dispose();
    }

    // a small overlay that reads the last/current errand's health at a glance
    _drawTrait(cr, cx) {
        const t = this._trait;
        if (t === 'blush') {                            // warm cheeks: snappy
            cr.setSourceRGBA(...PINK, 0.4);
            for (const s of [-1, 1]) {
                cr.save();
                cr.translate(cx + s * 25, 26);
                cr.scale(1.5, 1);
                cr.arc(0, 0, 5, 0, 2 * Math.PI);
                cr.restore();
                cr.fill();
            }
        }
        if (t === 'bolt') {                             // a yellow ⚡, like the CLI's: answered from memory
            cr.setSourceRGBA(1, 0.84, 0.3, 0.95);
            for (const [px, py] of [[6, 0], [0, 9], [4, 9], [2, 17], [9, 6], [5, 6], [8, 0]])
                cr.lineTo(cx + 39 + px, py);
            cr.closePath();
            cr.fill();
        }
        if (t === 'sweat') {                            // a bead of effort: slow link
            cr.setSourceRGBA(0.55, 0.78, 0.96, 0.92);
            const tx = cx + 42, ty = 1;
            cr.moveTo(tx, ty);
            cr.curveTo(tx - 4, ty + 7, tx - 4, ty + 12, tx, ty + 12);
            cr.curveTo(tx + 4, ty + 12, tx + 4, ty + 7, tx, ty);
            cr.fill();
        }
        if (t === 'scar') {                             // stitched scar: last errand broke
            cr.setSourceRGBA(...SCAR, 0.95);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.setLineWidth(1.6);
            const sx = cx - 34, sy = 2;
            cr.moveTo(sx, sy); cr.lineTo(sx + 7, sy + 15);
            cr.stroke();
            for (let i = 1; i <= 3; i++) {              // little cross-stitches
                const px = sx + (7 * i) / 4, py = sy + (15 * i) / 4;
                cr.moveTo(px - 3, py + 1.5); cr.lineTo(px + 3, py - 1.5);
            }
            cr.stroke();
        }
        if (t === 'dots') {                             // "…": waiting on Gemini
            cr.setSourceRGBA(1, 1, 1, 0.9);
            for (const i of [0, 1, 2]) {
                cr.arc(cx + 38 + i * 5, 4, 1.6, 0, 2 * Math.PI);
                cr.fill();
            }
        }
        if (t === 'signal' || t === 'nosignal') {       // weak signal: only the inner bar lit; none: all dim, struck out
            const sx = cx + 44, sy = 15;
            cr.setLineWidth(1.6);
            for (const [i, r] of [3, 7, 11].entries()) {
                cr.setSourceRGBA(1, 1, 1, t === 'signal' && i === 0 ? 0.95 : 0.3);
                cr.arc(sx, sy, r, -0.75 * Math.PI, -0.25 * Math.PI);
                cr.stroke();
            }
            if (t === 'nosignal') {
                cr.setSourceRGBA(...SCAR, 0.95);
                cr.moveTo(sx - 7, sy - 12);
                cr.lineTo(sx + 7, sy);
                cr.stroke();
            }
        }
        if (t === 'question' || t === 'zzz') {          // "?": no key that works; "zzz": the daemon is asleep
            cr.setSourceRGBA(1, 1, 1, 0.95);
            cr.selectFontFace('Sans', Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);
            const glyphs = t === 'question' ? [[38, 14, 14, '?']] : [[36, 18, 7, 'z'], [42, 12, 9, 'z'], [49, 7, 11, 'z']];
            for (const [gx, gy, size, ch] of glyphs) {
                cr.setFontSize(size);
                cr.moveTo(cx + gx, gy);
                cr.showText(ch);
            }
        }
    }

    _setTrait(t) {
        this._trait = t;
        this._mug.queue_repaint();
    }

    _setMood(mood, swing = mood === 'think' ? 280 : 0) {  // swing: ms per tail flick, faster under strain
        this._mood = mood;
        this._mug.queue_repaint();
        this._tail.remove_all_transitions();
        this._tail.rotation_angle_z = 0;
        if (swing && St.Settings.get().enable_animations) {
            this._tail.rotation_angle_z = -8;
            this._tail.ease({rotation_angle_z: 8, duration: swing, mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeatCount: -1, autoReverse: true});
        }
    }

    // the face while waiting: a known reason picks its own; otherwise time walks the WORK steps
    _wait() {
        const ms = (GLib.get_monotonic_time() - this._t0) / 1000;
        const [mood, trait, swing] = WAIT[this._why] ?? WORK.findLast(([after]) => ms >= after).slice(1);
        if (`${mood} ${trait}` === this._waitKey)
            return;
        this._waitKey = `${mood} ${trait}`;
        this._setMood(mood, swing);
        this._setTrait(trait);
    }

    // a different face each time it scurries in
    _greet() {
        const moods = ['idle', 'happy', 'wide', 'sleepy', 'curious'];
        const g = moods[Math.floor(Math.random() * moods.length)];
        this._lookX = g === 'curious' ? (Math.random() < 0.5 ? -1 : 1) : 0;
        this._setMood(g);
        this._setTrait('none');
        // wear the greeting for a moment, then settle to idle (where it blinks)
        this._timer('relax', 1800, () => {
            if (!this._run && !this._pending && this.actor.visible)
                this._setMood('idle');
            return GLib.SOURCE_REMOVE;
        });
    }

    _scheduleBlink() {
        this._timer('blink', 2500 + Math.random() * 3500, () => {
            this._blink();
            return GLib.SOURCE_REMOVE;
        });
    }

    _blink() {
        if (!this.actor.visible)
            return;
        if (SHUT.includes(this._mood)) {  // no round eyes to blink
            this._scheduleBlink();
            return;
        }
        this._blinking = true;
        this._mug.queue_repaint();
        this._timer('blinkoff', 140, () => {
            this._blinking = false;
            this._mug.queue_repaint();
            this._scheduleBlink();
            return GLib.SOURCE_REMOVE;
        });
    }

    // --- text ---------------------------------------------------------------

    _setStatus(text, style = '') {  // style: '', 'error' or 'hint'
        this._status.text = text;
        this._status.visible = !!text;
        for (const s of ['error', 'hint']) {
            if (s === style)
                this._status.add_style_class_name(`mus-status-${s}`);
            else
                this._status.remove_style_class_name(`mus-status-${s}`);
        }
    }

    _setAnswer(text) {
        this._answer = text;
        this._label.clutter_text.set_markup(markup(text.trim()));
        this._scroll.visible = !!text.trim();
    }

    _copy() {
        const code = this._answer.match(/`([^`\n]+)`/);
        const text = code ? code[1] : this._answer.trim();
        if (!text)
            return Clutter.EVENT_PROPAGATE;
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
        this._setStatus(code ? `Copied ${text}` : 'Copied the answer');
        return Clutter.EVENT_STOP;
    }

    // --- input --------------------------------------------------------------

    _onKey(ev) {
        const sym = ev.get_key_symbol();
        if (sym === Clutter.KEY_Escape) {
            if (this._pending)
                this._reply(false);
            else
                this.hide();
            return Clutter.EVENT_STOP;
        }
        const ctrl = ev.get_state() & Clutter.ModifierType.CONTROL_MASK;
        if (ctrl && (sym === Clutter.KEY_r || sym === Clutter.KEY_R)) {
            this._submit(true);
            return Clutter.EVENT_STOP;
        }
        if (ctrl && (sym === Clutter.KEY_d || sym === Clutter.KEY_D) && this._details) {  // the face said something's wrong; say what
            if (this._status.text === this._details)
                this._setStatus('Ctrl+D for details', 'hint');
            else
                this._setStatus(this._details, 'error');
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    _onClickOutside(ev) {
        const [ex, ey] = ev.get_coords();
        const [x, y] = this.actor.get_transformed_position();
        const [w, h] = this.actor.get_transformed_size();
        if (ex < x || ey < y || ex > x + w || ey > y + h) {
            this.hide();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    // --- talking to the daemon ----------------------------------------------

    _submit(fresh) {
        const prompt = this._entry.text.trim() || (fresh ? this._lastPrompt : '');
        if (!prompt)
            return;
        this._lastPrompt = prompt;
        this._cancel();
        this._clearTimer('autohide');
        this._ran = false;
        this._details = '';
        this._setAnswer('');
        this._setStatus('Thinking');
        this._clearTimer('relax');       // stay thinking; don't settle to idle mid-errand
        this._t0 = GLib.get_monotonic_time();
        this._gotFirst = false;
        this._why = '';
        this._waitKey = '';
        this._wait();
        const run = this._send({prompt, fresh, interactive: true}, ev => this._onEvent(ev, run), () => {
            if (this._run === run && !run.finished)
                this._fail('Mus stopped before finishing. Check it with: journalctl --user -u mus');
        });
        this._run = run;
        this._timer('mood', 500, () => {   // the face moves along as the wait gets longer
            if (this._run === run && !this._pending)
                this._wait();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _onEvent(ev, run) {
        if (this._run !== run)
            return;
        if (!this._gotFirst && (['text', 'run', 'approve', 'done'].includes(ev.t) || ev.why === 'work')) {
            this._gotFirst = true;
            this._ttftMs = (GLib.get_monotonic_time() - this._t0) / 1000;  // time to first token
        }
        if (ev.t === 'wait') {
            // 'work' means the model started: a slow start, the network or a key swap is behind it now.
            // A retry keeps its face until the next script runs.
            const why = ev.why === 'work' ? (this._why === 'retry' ? 'retry' : '') : ev.why;
            if (why && why !== this._why)
                this._whyAt = GLib.get_monotonic_time();
            if (ev.v)
                this._setStatus(ev.v);
            else if (this._why && !why)
                this._setStatus('Thinking');
            this._why = why;
            this._wait();
        } else if (ev.t === 'text') {
            this._setAnswer(this._answer + ev.v);
        } else if (ev.t === 'run') {
            this._ran = true;
            this._why = '';
            const line = ev.cmd.trim().split('\n').find(l => !/^set -e/.test(l.trim())) || '';
            this._setStatus(`Running ${line.length > 60 ? `${line.slice(0, 60)}…` : line}`);
        } else if (ev.t === 'approve') {
            this._pending = true;
            this._approveCmd.text = ev.cmd.trim().split('\n').slice(0, 6).join('\n');
            this._approve.show();
            this._setMood('wide');
            this._setStatus('');
            this.show();
            this._entry.grab_key_focus();
        } else if (ev.t === 'done') {
            run.finished = true;
            this._run = null;
            if (ev.v)
                this._setAnswer(ev.v);
            const trait = ev.cached ? 'bolt'
                : this._ttftMs < 600 ? 'blush' : this._ttftMs > 1800 ? 'sweat' : 'none';
            const happy = () => {
                this._setMood('happy');
                this._setTrait(trait);
                return GLib.SOURCE_REMOVE;
            };
            // a reason face that only just showed (a quick retry) stays up long enough to be seen
            const shown = this._why ? (GLib.get_monotonic_time() - this._whyAt) / 1000 : SHOW_MS;
            if (shown < SHOW_MS) {
                this._timer('mood', SHOW_MS - shown, happy);
            } else {
                this._clearTimer('mood');
                happy();
            }
            this._setStatus(ev.cached ? 'Remembered. Ctrl+R asks again' : this._ran ? 'Done' : '');
            if (!this.actor.visible)
                Main.notify('Mus', this._answer.trim() || 'Done');
            else if (this._ran)
                this._autoHide();
        } else if (ev.t === 'error') {
            run.finished = true;
            this._run = null;
            this._fail(ev.v, ev.why);
        }
    }

    _fail(msg, why = '') {
        this._run = null;
        this._clearTimer('mood');
        this._trouble(why, msg);
        if (!this.actor.visible)
            Main.notify('Mus', msg);
    }

    // something's wrong: the face says what kind of wrong, and Ctrl+D gives the words
    _trouble(why, details) {
        this._clearTimer('relax');
        const [mood, trait] = FAIL[why] ?? ['sad', 'scar'];
        this._setMood(mood);
        this._setTrait(trait);
        this._details = details;
        this._setStatus('Ctrl+D for details', 'hint');
    }

    // while idle, the face shows whether there's a network: offline, only remembered answers work
    _checkNet() {
        if (!this.actor.visible || this._run || this._pending)
            return;
        if (!this._net.network_available) {
            this._trouble('network', OFFLINE);
        } else if (this._details === OFFLINE) {
            this._details = '';
            this._setStatus('');
            this._setMood('idle');
            this._setTrait('none');
        }
    }

    _reply(ok) {
        if (!this._pending || !this._run?.conn)
            return;
        this._pending = false;
        this._approve.hide();
        this._t0 = GLib.get_monotonic_time();  // time spent deciding isn't Mus being slow
        this._waitKey = '';
        this._wait();
        this._setStatus(ok ? 'Allowed for this errand' : 'Denied');
        this._write(this._run, {approve: ok});
    }

    _autoHide() {
        this._timer('autohide', AUTO_HIDE_MS, () => {
            if (this.actor.hover)
                return GLib.SOURCE_CONTINUE;
            this.hide();
            return GLib.SOURCE_REMOVE;
        });
    }

    // one JSON request out, JSON-line events back; never blocks the shell
    _send(req, onEvent, onEnd) {
        const run = {cancellable: new Gio.Cancellable(), conn: null, finished: false};
        this._client.connect_async(Gio.UnixSocketAddress.new(SOCK), run.cancellable, (c, res) => {
            try {
                run.conn = c.connect_finish(res);
            } catch (e) {
                if (run.cancellable.is_cancelled())
                    return;
                if (!req.warm)
                    this._fail(DOWN, 'down');
                else if (!this._run && !this._pending)  // found out on opening: show it before the prompt is typed
                    this._trouble('down', DOWN);
                return;
            }
            this._write(run, req);
            const input = Gio.DataInputStream.new(run.conn.get_input_stream());
            const next = () => input.read_line_async(GLib.PRIORITY_DEFAULT, run.cancellable, (s, r) => {
                let line;
                try {
                    [line] = s.read_line_finish_utf8(r);
                } catch (e) {
                    if (!run.cancellable.is_cancelled())
                        this._fail(`Lost Mus mid-errand: ${e.message}`);
                    return;
                }
                if (line === null) {
                    run.conn.close(null);
                    onEnd();
                    return;
                }
                try {
                    onEvent(JSON.parse(line));
                } catch (e) {
                    logError(e, 'mus');
                }
                next();
            });
            next();
        });
        return run;
    }

    _write(run, obj) {
        const bytes = new GLib.Bytes(new TextEncoder().encode(`${JSON.stringify(obj)}\n`));
        run.conn.get_output_stream().write_bytes_async(bytes, GLib.PRIORITY_DEFAULT, run.cancellable, (s, r) => {
            try {
                s.write_bytes_finish(r);
            } catch (e) {
                if (!run.cancellable.is_cancelled())
                    logError(e, 'mus write');
            }
        });
    }

    _cancel() {
        if (this._run) {
            this._run.cancellable.cancel();
            this._run.conn?.close(null);
            this._run = null;
        }
        this._clearTimer('mood');
        this._pending = false;
        this._approve.hide();
    }

    _timer(name, ms, fn) {
        this._clearTimer(name);
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, fn);
        this._timers.add(id);
        this[`_${name}Id`] = id;
    }

    _clearTimer(name) {
        const id = this[`_${name}Id`];
        if (id && this._timers.delete(id))
            GLib.source_remove(id);
        this[`_${name}Id`] = 0;
    }

    destroy() {
        this._net.disconnect(this._netId);
        this._cancel();
        for (const id of this._timers)
            GLib.source_remove(id);
        this._timers.clear();
        if (this._grab)
            Main.popModal(this._grab);
        this.actor.destroy();
    }
}

export default class MusExtension extends Extension {
    enable() {
        this._mouse = new Mouse();
        Main.layoutManager.addTopChrome(this._mouse.actor);

        this._button = new PanelMenu.Button(0.0, 'Mus', true);
        this._button.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${this.path}/icons/mus-symbolic.svg`),
            style_class: 'system-status-icon',
        }));
        this._button.connect('button-press-event', () => {
            this._mouse.toggle();
            return Clutter.EVENT_STOP;
        });
        Main.panel.addToStatusArea(this.uuid, this._button);

        Main.wm.addKeybinding('mus-toggle', this.getSettings(), Meta.KeyBindingFlags.NONE,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP, () => this._mouse.toggle());
    }

    disable() {
        Main.wm.removeKeybinding('mus-toggle');
        this._button?.destroy();
        this._button = null;
        if (this._mouse) {
            Main.layoutManager.removeChrome(this._mouse.actor);
            this._mouse.destroy();
        }
        this._mouse = null;
    }
}
