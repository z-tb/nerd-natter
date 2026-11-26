# Nerd Natter dictation. Everything here is a thin wrapper around ./nn-ctl, which the
# Cinnamon applet uses too. Settings live in ~/.config/nerd-natter/config.env.
# Give a value here to override it for one run:  make run MODEL_DIR=/other/model
#
#   make build       build the Docker image (or the Python environment, in venv mode)
#   make install     add the panel applet, icons and menu entry
#   make run         start dictation (the service and the key watcher)
#   make stop        stop dictation
#   make uninstall   remove the applet, icons, menu entry and autostart entry
#
# First time? ./install.sh asks questions and sets everything up for you.

CTL = $(CURDIR)/nn-ctl
APPLET_UUID = nn@nerd-natter
APPLET_DIR  = $(HOME)/.local/share/cinnamon/applets
ICON_DIR    = $(HOME)/.local/share/icons/hicolor
LAUNCHER    = $(HOME)/.local/share/applications/nerd-natter.desktop
AUTOSTART   = $(HOME)/.config/autostart/nerd-natter.desktop
CONF_DIR    = $(HOME)/.config/nerd-natter
# Names used before the project was called Nerd Natter (removed on install and uninstall)
OLD = $(APPLET_DIR)/ptt@nerd-dictation $(HOME)/.local/share/applications/nerd-dictation-ptt.desktop \
      $(HOME)/.config/autostart/nerd-dictation-ptt.desktop

.DEFAULT_GOAL := help
.PHONY: help build install run stop uninstall status logs

help:
	@sed -n 's/^#   make /  make /p' $(firstword $(MAKEFILE_LIST))

build:
	@$(CTL) sync-version >/dev/null
	@if [ "$$($(CTL) get RUN_MODE)" = venv ]; then $(CTL) setup-venv; else $(CTL) build; fi

install:
	chmod 700 $(CTL) $(CURDIR)/install.sh $(CURDIR)/nn_watch.py   # only you can run the scripts
	@bash $(CTL) sync-version >/dev/null
	rm -f $(OLD)
	-find $(ICON_DIR) -name 'nerd-dictation-ptt.*' -delete
	mkdir -p $(APPLET_DIR) $(ICON_DIR) $(CONF_DIR) $(dir $(LAUNCHER))
	cp -r $(CURDIR)/icons/hicolor/. $(ICON_DIR)/
	-gtk-update-icon-cache -f -t $(ICON_DIR)
	ln -sfn $(CURDIR)/applet/$(APPLET_UUID) $(APPLET_DIR)/$(APPLET_UUID)
	test -f $(CONF_DIR)/config.env || printf '%s\n' \
		'# Nerd Natter settings. The applet Configure window edits this file too.' \
		'PROJECT_DIR=$(CURDIR)' > $(CONF_DIR)/config.env
	printf '%s\n' \
		'[Desktop Entry]' \
		'Type=Application' \
		'Name=Nerd Natter' \
		'Comment=nerd-dictation push-to-talk' \
		"Exec=bash '$(CTL)' start" \
		'Icon=nerd-natter' \
		'Terminal=false' \
		'Categories=Utility;Accessibility;' \
		'Actions=Stop;' \
		'' \
		'[Desktop Action Stop]' \
		'Name=Stop Nerd Natter' \
		"Exec=bash '$(CTL)' stop" \
		> $(LAUNCHER)
	-update-desktop-database $(dir $(LAUNCHER))
	@echo "Installed. Add the applet: right-click the panel > Applets > Nerd Natter."
	@echo "Not listed? Reload Cinnamon (Alt+F2, r, Enter)."

run:
	@$(CTL) start

stop:
	@$(CTL) stop

uninstall: stop
	rm -f $(APPLET_DIR)/$(APPLET_UUID) $(LAUNCHER) $(AUTOSTART) $(OLD)
	-find $(ICON_DIR) \( -name 'nerd-natter.*' -o -name 'nerd-dictation-ptt.*' \) -delete
	-gtk-update-icon-cache -f -t $(ICON_DIR)
	-update-desktop-database $(dir $(LAUNCHER))
	@echo "Removed. Your settings ($(CONF_DIR)) and models were left alone."

status:
	@$(CTL) status

logs:
	@$(CTL) logs
