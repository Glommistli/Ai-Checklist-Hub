"""Open the isolated UI fixture in the same WebView2 engine as the desktop app."""
import webview
webview.create_window("Checklist UI QA", "http://127.0.0.1:4178", width=1180, height=820, min_size=(390, 560))
webview.start(gui="edgechromium")
