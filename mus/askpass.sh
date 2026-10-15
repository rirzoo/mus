#!/bin/sh
# SUDO_ASKPASS helper: only wired in after the user taps Allow
exec secret-tool lookup service mus key sudo
