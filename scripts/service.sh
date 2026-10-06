#!/bin/bash
set -e

# Manage the claudebot-server launchd service

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PLIST_NAME="com.claudebot.server.plist"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
ENV_FILE="$ROOT_DIR/.env"

usage() {
    echo "Usage: $0 {start|stop|restart|status|install|uninstall}"
    echo
    echo "Commands:"
    echo "  start      Start the service"
    echo "  stop       Stop the service"
    echo "  restart    Restart the service"
    echo "  status     Check if service is running"
    echo "  install    Generate plist and install service"
    echo "  uninstall  Stop and remove the service"
    exit 1
}

is_loaded() {
    launchctl list 2>/dev/null | grep -q com.claudebot.server
}

load_env_vars() {
    # Parse .env file and output plist EnvironmentVariables XML
    if [ ! -f "$ENV_FILE" ]; then
        echo "Error: .env file not found at $ENV_FILE"
        echo "Copy .env.example to .env and fill in your values"
        exit 1
    fi

    local env_xml=""
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip comments and empty lines
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue

        # Parse KEY=VALUE
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            # Remove surrounding quotes if present
            value="${value#\"}"
            value="${value%\"}"
            value="${value#\'}"
            value="${value%\'}"
            env_xml+="        <key>$key</key>
        <string>$value</string>
"
        fi
    done < "$ENV_FILE"

    echo "$env_xml"
}

do_install() {
    # Find uv binary
    UV_PATH=$(which uv 2>/dev/null || echo "$HOME/.local/bin/uv")
    if [ ! -x "$UV_PATH" ]; then
        echo "Error: uv not found. Install it from https://github.com/astral-sh/uv"
        exit 1
    fi

    # Create logs directory
    mkdir -p "$ROOT_DIR/logs"

    # Load environment variables from .env
    ENV_VARS=$(load_env_vars)

    # Generate plist
    echo "==> Generating $PLIST_NAME"
    cat > "$ROOT_DIR/$PLIST_NAME" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claudebot.server</string>

    <key>ProgramArguments</key>
    <array>
        <string>$UV_PATH</string>
        <string>run</string>
        <string>claude-bot</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>$ROOT_DIR/logs/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$ROOT_DIR/logs/stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/opt/libpq/bin:/opt/homebrew/bin:$HOME/.nvm/versions/node/v22.11.0/bin:$HOME/.local/bin:$(dirname "$UV_PATH"):/usr/local/bin:/usr/bin:/bin</string>
$ENV_VARS    </dict>
</dict>
</plist>
EOF

    # Install plist
    mkdir -p "$LAUNCH_AGENTS_DIR"
    cp "$ROOT_DIR/$PLIST_NAME" "$PLIST_PATH"
    echo "==> Installed to $PLIST_PATH"
}

do_start() {
    if ! [ -f "$PLIST_PATH" ]; then
        echo "Service not installed. Run: $0 install"
        exit 1
    fi
    if is_loaded; then
        echo "Service already running"
    else
        launchctl load "$PLIST_PATH"
        echo "Service started"
    fi
}

do_stop() {
    if is_loaded; then
        launchctl unload "$PLIST_PATH"
        echo "Service stopped"
    else
        echo "Service not running"
    fi
}

do_restart() {
    do_stop
    sleep 1
    do_start
}

do_status() {
    echo "==> Service status"
    if is_loaded; then
        echo "Service: running"
        launchctl list | grep com.claudebot.server
    else
        echo "Service: not running"
    fi

    echo
    echo "==> Recent logs (last 10 lines)"
    if [ -f "$ROOT_DIR/logs/stdout.log" ]; then
        tail -10 "$ROOT_DIR/logs/stdout.log"
    else
        echo "No logs found"
    fi
}

do_uninstall() {
    echo "==> Uninstalling service"
    if is_loaded; then
        launchctl unload "$PLIST_PATH"
        echo "Service stopped"
    fi
    if [ -f "$PLIST_PATH" ]; then
        rm "$PLIST_PATH"
        echo "Removed $PLIST_PATH"
    fi
    if [ -f "$ROOT_DIR/$PLIST_NAME" ]; then
        rm "$ROOT_DIR/$PLIST_NAME"
        echo "Removed $ROOT_DIR/$PLIST_NAME"
    fi
    echo "==> Uninstalled"
}

case "${1:-}" in
    start)     do_start ;;
    stop)      do_stop ;;
    restart)   do_restart ;;
    status)    do_status ;;
    install)   do_install && do_start ;;
    uninstall) do_uninstall ;;
    *)         usage ;;
esac
