#!/bin/bash
# AI Portfolio Trader - Background Startup Script
# Usage: ./start.sh        (start the server)
#        ./start.sh stop   (stop the server)
#        ./start.sh status (check if running)

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PIDFILE="$DIR/.server.pid"
LOGFILE="$DIR/ai-trader.log"

case "${1:-start}" in
    start)
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "Server already running (PID $(cat "$PIDFILE"))"
            echo "Dashboard: http://127.0.0.1:8000"
            exit 0
        fi
        source venv/bin/activate
        nohup python main.py >> "$LOGFILE" 2>&1 &
        echo $! > "$PIDFILE"
        sleep 2
        if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "Server started (PID $(cat "$PIDFILE"))"
            echo "Dashboard: http://127.0.0.1:8000"
            echo "Agents run daily at 9:25 AM ET (Mon-Fri)"
            echo "Log: $LOGFILE"
        else
            echo "Failed to start. Check $LOGFILE"
            exit 1
        fi
        ;;
    stop)
        if [ -f "$PIDFILE" ]; then
            kill "$(cat "$PIDFILE")" 2>/dev/null
            rm -f "$PIDFILE"
            echo "Server stopped"
        else
            echo "No server running"
        fi
        ;;
    status)
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "Server running (PID $(cat "$PIDFILE"))"
            echo "Dashboard: http://127.0.0.1:8000"
        else
            echo "Server not running"
            rm -f "$PIDFILE"
        fi
        ;;
    *)
        echo "Usage: ./start.sh [start|stop|status]"
        ;;
esac
