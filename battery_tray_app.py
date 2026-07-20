import time
import threading
from typing import Optional
import pystray
from PIL import Image, ImageDraw, ImageFont

from battery_reader import load_cue_dll, CueSdkClient, LOG


class BatteryTrayApp:
    def __init__(self):
        self.icon = None
        self.client = None
        self.dll = None
        self.devices = []
        self.current_battery = None
        self.previous_battery = None
        self.running = False
        self.update_thread = None
        
        # Notification thresholds and tracking
        self.low_threshold = 20
        self.high_threshold = 80
        self.notified_low = False
        self.notified_high = False

    def setup(self):
        """Initialize SDK connection."""
        try:
            self.dll = load_cue_dll()
            self.client = CueSdkClient(self.dll)
            self.client.connect()
            LOG.info("Connected to iCUE SDK")
            self.devices = self.client.get_devices()
            LOG.info(f"Found {len(self.devices)} devices")
            return True
        except Exception as e:
            LOG.error(f"Failed to setup: {e}")
            return False

    def get_battery_info(self):
        """Get battery info from all wireless devices."""
        if not self.client or not self.devices:
            return None

        battery_devices = []
        for device in self.devices:
            battery = self.client.read_battery_property(device.device_id)
            if battery is not None:
                battery_devices.append({
                    'model': device.model,
                    'battery': battery
                })

        return battery_devices

    def check_battery_thresholds(self, battery_level: int):
        """Check battery level and send notifications if thresholds crossed."""
        if self.icon is None:
            return
        
        device_name = self.devices[0].model if self.devices else "Device"
        
        # Check LOW threshold
        if battery_level <= self.low_threshold and not self.notified_low:
            self.icon.notify(
                title="⚠️ Low Battery Warning",
                message=f"{device_name} battery is at {battery_level}%\nPlease charge soon!"
            )
            self.notified_low = True
            self.notified_high = False  # Reset high notification
            LOG.warning(f"Low battery notification sent: {battery_level}%")
        
        # Check HIGH threshold
        elif battery_level >= self.high_threshold and not self.notified_high:
            self.icon.notify(
                title="🔋 Battery Charged",
                message=f"{device_name} battery is at {battery_level}%\nYou can unplug now."
            )
            self.notified_high = True
            self.notified_low = False  # Reset low notification
            LOG.info(f"High battery notification sent: {battery_level}%")
        
        # Reset notifications when battery is in the middle range (21-79%)
        elif self.low_threshold < battery_level < self.high_threshold:
            self.notified_low = False
            self.notified_high = False

    def update_battery(self):
        """Update battery level (called periodically)."""
        try:
            battery_info = self.get_battery_info()
            if battery_info and len(battery_info) > 0:
                # Store previous value
                self.previous_battery = self.current_battery
                
                # Use first wireless device
                self.current_battery = battery_info[0]['battery']
                LOG.info(f"Battery: {self.current_battery}%")
                
                # Check thresholds (only if battery level changed)
                if self.previous_battery != self.current_battery:
                    self.check_battery_thresholds(self.current_battery)
            else:
                self.current_battery = None
        except Exception as e:
            LOG.error(f"Error updating battery: {e}")
            self.current_battery = None

        # Update icon
        if self.icon:
            self.icon.icon = create_battery_icon(self.current_battery)
            
            # Update tooltip
            if self.current_battery is not None:
                device_name = self.devices[0].model if self.devices else "Device"
                self.icon.title = f"{device_name}: {self.current_battery}%"
            else:
                self.icon.title = "Corsair Battery (No wireless devices)"

    def update_loop(self):
        """Background thread that updates battery periodically."""
        while self.running:
            self.update_battery()
            # Wait 60 seconds between updates
            for _ in range(60):
                if not self.running:
                    break
                time.sleep(1)

    def on_refresh(self, icon, item):
        """Handle refresh menu item - update battery and show notification."""
        try:
            LOG.info("Manual refresh triggered")
            
            # Get fresh battery data
            battery_info = self.get_battery_info()
            
            if battery_info and len(battery_info) > 0:
                # Store old value
                self.previous_battery = self.current_battery
                
                # Update current battery
                self.current_battery = battery_info[0]['battery']
                LOG.info(f"Manual refresh: {self.current_battery}%")
                
                # Check thresholds if battery changed
                if self.previous_battery != self.current_battery:
                    self.check_battery_thresholds(self.current_battery)
                
                # Update icon immediately
                if self.icon:
                    self.icon.icon = create_battery_icon(self.current_battery)
                    device_name = self.devices[0].model if self.devices else "Device"
                    self.icon.title = f"{device_name}: {self.current_battery}%"
                
                # Always show status notification when clicked
                message = "\n".join([f"{d['model']}: {d['battery']}%" for d in battery_info])
                icon.notify("🔋 Battery Status", message)
                
            else:
                icon.notify("Battery Status", "No wireless devices found")
                
        except Exception as e:
            LOG.error(f"Error during manual refresh: {e}")
            icon.notify("Error", "Failed to refresh battery info")

    def on_clicked(self, icon, item):
        """Handle direct icon click (left click on icon itself)."""
        self.on_refresh(icon, item)

    def on_quit(self, icon, item):
        """Handle quit menu item."""
        self.running = False
        icon.stop()

    def run(self):
        """Run the system tray application."""
        if not self.setup():
            print("Failed to connect to iCUE SDK. Make sure iCUE is running and SDK is enabled.")
            return

        self.running = True
        
        # Get initial battery level
        self.update_battery()

        # Create system tray icon with click handler
        menu = pystray.Menu(
            pystray.MenuItem("Refresh", self.on_refresh, default=True),
            pystray.MenuItem("Quit", self.on_quit)
        )

        self.icon = pystray.Icon(
            "corsair_battery",
            create_battery_icon(self.current_battery),
            "Corsair Battery",
            menu,
            on_click=self.on_clicked  # Handle left click on icon
        )

        # Start update thread
        self.update_thread = threading.Thread(target=self.update_loop, daemon=True)
        self.update_thread.start()

        # Run icon (blocks until quit)
        try:
            self.icon.run()
        finally:
            self.running = False
            if self.client:
                self.client.disconnect()
            LOG.info("Application closed")


def create_battery_icon(percentage: Optional[int], size=64):
    """Create a system tray icon showing battery percentage."""
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    if percentage is None:
        # Show "?" for unknown
        try:
            font = ImageFont.truetype("arial.ttf", size // 2)
        except:
            font = ImageFont.load_default()
        draw.text((size // 2, size // 2), "?", fill=(128, 128, 128, 255), anchor="mm", font=font)
    else:
        # Determine color based on battery level
        if percentage >= 60:
            color = (0, 200, 0, 255)  # Green
        elif percentage >= 30:
            color = (255, 165, 0, 255)  # Orange
        else:
            color = (255, 0, 0, 255)  # Red
        
        # Draw text
        text = str(percentage)
        try:
            font = ImageFont.truetype("arial.ttf", size // 1.5)
        except:
            font = ImageFont.load_default()
        
        draw.text((size // 2, size // 2), text, fill=color, anchor="mm", font=font)
    
    return image


def main():
    app = BatteryTrayApp()
    app.run()


if __name__ == "__main__":
    main()