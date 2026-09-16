"""HDMI expression display for Nuanyu robot car.

The 5-inch HDMI screen is driven as a fullscreen Firefox kiosk window
pointing at http://127.0.0.1:5004/display.  The page polls /api/status
and maps visual_state + emotion to animated face expressions.

Startup sequence (handled by launch_display.sh):
  1. Start Weston compositor on the HDMI output
  2. Start Firefox in kiosk mode → /display
"""
