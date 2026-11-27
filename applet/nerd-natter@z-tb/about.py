#!/usr/bin/env python3
"""The "Help and website" window for the Nerd Natter applet (a normal GTK About window,
so the web address is a real clickable link)."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = "https://github.com/z-tb/nerd-natter"

try:
    with open(os.path.join(HERE, "metadata.json"), encoding="utf-8") as f:
        meta = json.load(f)
except (OSError, ValueError):
    meta = {}

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GdkPixbuf, Gtk
except Exception:
    # No GTK for Python on this computer: open the project page instead
    subprocess.Popen(["xdg-open", SITE])
    sys.exit(0)

dialog = Gtk.AboutDialog()
dialog.set_program_name(meta.get("name", "Nerd Natter"))
dialog.set_version(meta.get("version", ""))
dialog.set_comments(
    "Hold a key, talk, and your words are typed wherever your cursor is.\n"
    "Powered by nerd-dictation and Vosk. Runs locally on your computer.\n\n"
    "Questions, ideas and problems are welcome on the project page.")
dialog.set_website(SITE)
dialog.set_website_label("github.com/z-tb/nerd-natter")
dialog.set_authors(["z-tb"])
dialog.set_license_type(Gtk.License.GPL_3_0)
icon = os.path.join(HERE, "icon.png")
if os.path.exists(icon):
    dialog.set_logo(GdkPixbuf.Pixbuf.new_from_file_at_size(icon, 64, 64))
dialog.set_title("Help and website")
dialog.run()
