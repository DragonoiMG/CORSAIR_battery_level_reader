#!/usr/bin/env python3
"""
cue_battery_reader.py

Read Corsair device battery level using the official iCUE SDK (C API) via ctypes.

Principles followed:
- single-responsibility small functions
- explicit error handling and informative logs
- type hints and small dataclasses where helpful
- minimal dependencies (only Python stdlib + ctypes)

Usage examples:
    python cue_battery_reader.py         # list devices and read battery if available
    python cue_battery_reader.py --device-index 0  # target device by index
    python cue_battery_reader.py --poll 2  # poll every 2 seconds (Ctrl-C to stop)

Notes:
- Place the SDK runtime DLL in PATH (from SDK/redist). For 64-bit Python use the 64-bit DLL.
- iCUE must be running.
"""
from __future__ import annotations
import ctypes
from ctypes import WINFUNCTYPE, c_void_p, c_int
import ctypes.util
import sys
import argparse
import time
import logging
from dataclasses import dataclass
from typing import List, Optional
import os
import platform
import threading

LOG = logging.getLogger("cue_battery_reader")
CORSAIR_DLL_NAMES = ("iCUESDK\\redist\\x64\\iCUESDK.x64_2019.dll")  # try common names

# From SDK docs:
CDPI_BatteryLevel = 9  # CorsairDevicePropertyId for battery level (0-100)
# CorsairDataType items (we only need Int32 & String here)
CT_Int32 = 1
CT_String = 3

# Error codes from SDK
CE_Success = 0
CE_ServerNotFound = 1
CE_NoControl = 2
CE_IncompatibleProtocol = 3
CE_InvalidArguments = 4
CE_InvalidOperation = 5
CE_DeviceNotFound = 6
CE_NotAllowed = 7

# Session states from SDK
CSS_Invalid = 0
CSS_Closed = 1
CSS_Connecting = 2
CSS_Timeout = 3
CSS_ConnectionRefused = 4
CSS_ConnectionLost = 5
CSS_Connected = 6

# Device type mask - CDT_All to get all devices
CDT_All = 0xFFFFFFFF
CDT_Headset = 0x0008

# (We treat eventData as opaque pointer for now)
CorsairSessionStateChangedHandler = WINFUNCTYPE(None, c_void_p, c_void_p)

# Reasonably sized constants from SDK (docs mention sizes like CORSAIR_STRING_SIZE_M)
CORSAIR_STRING_SIZE_M = 128  # from SDK header: const unsigned int CORSAIR_STRING_SIZE_M = 128
CORSAIR_DEVICE_COUNT_MAX = 64


@dataclass
class DeviceInfo:
    idx: int
    device_type: int
    device_id: str
    serial: str
    model: str
    led_count: int
    channel_count: int


class CUEError(Exception):
    pass


def load_cue_dll() -> ctypes.CDLL:
    """
    Robust loader for the iCUE/CUESDK DLL.

    Search order:
    1. Environment variable CUE_SDK_DLL (full path recommended)
    2. A set of likely relative/absolute paths (SDK redist folders)
    3. ctypes.util.find_library fallback (less reliable on Windows)

    Raises a RuntimeError with actionable debug text on failure.
    """
    candidates = [r"\path\to\your\x64\iCUESDK.x64_2019.dll"]

    # 1) Allow user to provide an explicit DLL path via env var
    env_path = os.environ.get("CUE_SDK_DLL")
    if env_path:
        candidates.append(env_path)

    # 2) Common redistributable filenames and relative SDK paths
    # Adjust these names to match the exact file you have in your SDK redist folder.
    common_names = [
        "iCUESDK.x64_2019.dll", "iCUESDK.x64.dll", "CUESDK.x64.dll",
        "CUESDK.dll", "iCUESDK.dll"
    ]
    rel_paths = [
        # relative to script cwd
        os.path.join("iCUESDK", "redist", "x64"),
        os.path.join("iCUESDK", "redist"),
        os.path.join("redist", "x64"),
        os.path.join("iCUESDK", "redist", "x86")  # just in case
    ]
    # add combos
    cwd = os.getcwd()
    for p in rel_paths:
        for name in common_names:
            candidates.append(os.path.join(cwd, p, name))
    # also try direct filenames (rely on PATH)
    candidates.extend(common_names)

    # 3) ctypes.find_library fallback (rarely helpful for redistributable dlls)
    found = ctypes.util.find_library("iCUESDK") or ctypes.util.find_library("CUESDK")
    if found:
        candidates.append(found)

    # deduplicate while preserving order
    seen = set()
    candidates = [c for c in candidates if c and not (c in seen or seen.add(c))]

    last_exc = None
    tried = []
    for path in candidates:
        tried.append(path)
        # If path looks like an absolute/relative file path, prefer full-path attempt.
        if os.path.isabs(path) or os.path.exists(path):
            # confirm file exists before trying to load
            if not os.path.exists(path):
                # still try to load by name (maybe it is in PATH)
                try:
                    dll = ctypes.WinDLL(path)
                    return dll
                except Exception as e:
                    last_exc = e
                    continue
            try:
                dll = ctypes.WinDLL(path)
                return dll
            except Exception as e:
                last_exc = e
                # capture windows-specific error text if possible
                win_err = getattr(e, 'winerror', None)
                raise_info = f"{e!r}"
                # store and continue trying other candidates
                continue
        else:
            # try treating as a library name that loader will search via PATH
            try:
                dll = ctypes.WinDLL(path)
                return dll
            except Exception as e:
                last_exc = e
                continue

    # If we reach here, none succeeded. Build a helpful error message.
    bitness = platform.architecture()[0]
    err_lines = [
        "Could not load iCUE/CUESDK DLL. Tried the following candidates:",
        *["  - " + t for t in tried],
        "",
        f"Your Python process is: {bitness}. Make sure you use the matching iCUE DLL (x64 vs x86).",
        "Suggested fixes:",
        "  * Set the exact path on candidates variable or Set the exact DLL path via environment variable CUE_SDK_DLL, e.g.:",
        r'      set CUE_SDK_DLL=C:\path\to\iCUESDK.x64_2019.dll',
        "  * Or copy the correct SDK DLL into the same folder as your script.",
        "  * Install the Microsoft Visual C++ Redistributable (2015-2019/2022) x64 if missing.",
        "  * Ensure iCUE SDK redistributable DLL matches your Python bitness (64-bit Python needs x64 DLL).",
        "",
        f"Last loader exception: {last_exc!r}",
    ]
    raise RuntimeError("\n".join(err_lines))


# ---- ctypes struct definitions (inferred from SDK docs) ----
class CorsairDeviceId(ctypes.c_wchar_p):
    """Device id is a null-terminated Unicode string (wchar*)."""


class CorsairDeviceFilter(ctypes.Structure):
    """Device filter struct - contains device type mask."""
    _fields_ = [
        ("deviceTypeMask", ctypes.c_int),
    ]


class CorsairSessionStateChanged(ctypes.Structure):
    """Session state change event data."""
    _fields_ = [
        ("state", ctypes.c_int),  # CorsairSessionState
        # We're skipping the CorsairSessionDetails struct for simplicity
        # Add it here if you need version info
    ]


class CorsairDeviceInfoStruct(ctypes.Structure):
    # From SDK header: all fields are char arrays (UTF-8 encoded)
    _fields_ = [
        ("type", ctypes.c_int),                           # CorsairDeviceType (enum = int)
        ("id", ctypes.c_char * CORSAIR_STRING_SIZE_M),    # CorsairDeviceId typedef
        ("serial", ctypes.c_char * CORSAIR_STRING_SIZE_M),
        ("model", ctypes.c_char * CORSAIR_STRING_SIZE_M),
        ("ledCount", ctypes.c_int),
        ("channelCount", ctypes.c_int),
    ]

# Property union & struct (simplified to the types we need)
class CorsairDataType_StringArray(ctypes.Structure):
    _fields_ = [("items", ctypes.POINTER(ctypes.c_char_p)), ("count", ctypes.c_uint)]


class CorsairDataValueUnion(ctypes.Union):
    _fields_ = [
        ("int32", ctypes.c_int),
        ("float64", ctypes.c_double),
        ("string", ctypes.c_char_p),
        ("boolean", ctypes.c_uint8),  # store as byte
        ("int32_array", ctypes.POINTER(ctypes.c_int)),
        ("string_array", ctypes.POINTER(ctypes.c_char_p)),
    ]


class CorsairPropertyStruct(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),  # CorsairDataType
        ("value", CorsairDataValueUnion),
    ]


# ---- High-level wrapper functions ----
class CueSdkClient:
    def __init__(self, dll: ctypes.CDLL):
        self.dll = dll
        self._setup_prototypes()
        self._session_state = CSS_Closed
        self._state_lock = threading.Lock()
        self._state_event = threading.Event()

    def _setup_prototypes(self):
        """Define argtypes/restype for the subset of SDK functions we use."""
        # CorsairConnect(CorsairSessionStateChangedHandler onStateChanged, void* context) -> CorsairError (int)
        try:
            self.dll.CorsairConnect.restype = ctypes.c_int
            self.dll.CorsairConnect.argtypes = [CorsairSessionStateChangedHandler, ctypes.c_void_p]
        except AttributeError as e:
            raise CUEError("CUESDK missing CorsairConnect export") from e

        # CorsairDisconnect()
        self.dll.CorsairDisconnect.restype = ctypes.c_int
        self.dll.CorsairDisconnect.argtypes = []

        # CorsairGetDevices(const CorsairDeviceFilter*, int sizeMax, CorsairDeviceInfo* devices, int* size)
        # Use POINTER(CorsairDeviceFilter) for the filter parameter
        self.dll.CorsairGetDevices.restype = ctypes.c_int
        self.dll.CorsairGetDevices.argtypes = [ctypes.POINTER(CorsairDeviceFilter), ctypes.c_int,
                                               ctypes.c_void_p,  # devices array as void*
                                               ctypes.POINTER(ctypes.c_int)]

        # CorsairGetDeviceInfo(const CorsairDeviceId deviceId, CorsairDeviceInfo* deviceInfo)
        self.dll.CorsairGetDeviceInfo.restype = ctypes.c_int
        self.dll.CorsairGetDeviceInfo.argtypes = [ctypes.c_char_p, ctypes.POINTER(CorsairDeviceInfoStruct)]

        # CorsairGetDevicePropertyInfo(...)
        self.dll.CorsairGetDevicePropertyInfo.restype = ctypes.c_int
        self.dll.CorsairGetDevicePropertyInfo.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint,
                                                          ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint)]

        # CorsairReadDeviceProperty(...)
        # IMPORTANT: Last param is CorsairProperty*, not CorsairProperty**
        self.dll.CorsairReadDeviceProperty.restype = ctypes.c_int
        self.dll.CorsairReadDeviceProperty.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint,
                                                       ctypes.POINTER(CorsairPropertyStruct)]

        # CorsairFreeProperty(CorsairProperty* property)
        self.dll.CorsairFreeProperty.restype = ctypes.c_int
        self.dll.CorsairFreeProperty.argtypes = [ctypes.POINTER(CorsairPropertyStruct)]

    def _session_state_callback(self, context, event_data):
        """
        Callback invoked by SDK when session state changes.
        We parse the event data to track connection state.
        """
        try:
            # Cast the opaque event_data pointer to our struct
            if event_data:
                event = ctypes.cast(event_data, ctypes.POINTER(CorsairSessionStateChanged)).contents
                new_state = event.state
                with self._state_lock:
                    old_state = self._session_state
                    self._session_state = new_state
                    
                # Log the state transition
                state_names = {
                    CSS_Invalid: "Invalid",
                    CSS_Closed: "Closed",
                    CSS_Connecting: "Connecting",
                    CSS_Timeout: "Timeout",
                    CSS_ConnectionRefused: "ConnectionRefused",
                    CSS_ConnectionLost: "ConnectionLost",
                    CSS_Connected: "Connected"
                }
                old_name = state_names.get(old_state, f"Unknown({old_state})")
                new_name = state_names.get(new_state, f"Unknown({new_state})")
                LOG.info("Session state changed: %s -> %s", old_name, new_name)
                
                # Signal if we've reached connected state
                if new_state == CSS_Connected:
                    self._state_event.set()
                elif new_state in (CSS_Timeout, CSS_ConnectionRefused, CSS_ConnectionLost):
                    # Connection failed or lost
                    self._state_event.set()
        except Exception as e:
            LOG.error("Error in session state callback: %s", e)

    def connect(self, timeout: float = 5.0) -> None:
        """
        Connect to the iCUE server with a session-state callback.
        Waits for connection to complete or timeout.
        
        Args:
            timeout: Maximum seconds to wait for connection (default 5.0)
            
        Raises:
            CUEError: If connection fails or times out
        """
        # Reset state tracking
        with self._state_lock:
            self._session_state = CSS_Closed
        self._state_event.clear()
        
        # wrap Python method in a C callback and keep a reference to avoid GC
        self._cb_func = CorsairSessionStateChangedHandler(self._session_state_callback)
        
        # Initiate connection
        res = self.dll.CorsairConnect(self._cb_func, c_void_p(0))
        if res != CE_Success:
            raise CUEError(f"CorsairConnect failed (error code {res})")
        
        LOG.info("Connecting to iCUE SDK...")
        
        # Wait for connection to complete
        if not self._state_event.wait(timeout):
            raise CUEError(f"Connection timeout after {timeout} seconds. Is iCUE running?")
        
        # Check final state
        with self._state_lock:
            final_state = self._session_state
        
        if final_state == CSS_Connected:
            LOG.info("Successfully connected to iCUE SDK")
        elif final_state == CSS_Timeout:
            raise CUEError("Connection timed out. iCUE may not be running.")
        elif final_state == CSS_ConnectionRefused:
            raise CUEError("Connection refused. Enable 'SDK' in iCUE Settings → Software and Games")
        elif final_state == CSS_ConnectionLost:
            raise CUEError("Connection lost immediately after connecting")
        else:
            raise CUEError(f"Connection failed with state {final_state}")

    def disconnect(self) -> None:
        """Disconnect from iCUE SDK and cleanup."""
        try:
            self.dll.CorsairDisconnect()
            LOG.info("Disconnected from iCUE SDK")
        finally:
            # Reset state
            with self._state_lock:
                self._session_state = CSS_Closed
            self._state_event.clear()
            # drop callback reference so Python can GC it when client destroyed
            if hasattr(self, "_cb_func"):
                del self._cb_func

    def get_devices(self) -> List[DeviceInfo]:
        """
        Call CorsairGetDevices with proper filter struct.
        Returns list of DeviceInfo dataclass instances.
        
        Raises:
            CUEError: If not connected or SDK call fails
        """
        # Check connection state
        with self._state_lock:
            if self._session_state != CSS_Connected:
                raise CUEError("Not connected to iCUE. Call connect() first.")
        
        # Create a proper CorsairDeviceFilter struct
        # Use CDT_All (0xFFFFFFFF) to get all device types
        device_filter = CorsairDeviceFilter(deviceTypeMask=CDT_All)
        
        devices_array = (CorsairDeviceInfoStruct * CORSAIR_DEVICE_COUNT_MAX)()
        size = ctypes.c_int(0)

        # Pass pointer to the filter struct using ctypes.byref
        res = self.dll.CorsairGetDevices(
            ctypes.byref(device_filter),  # Pointer to filter struct
            CORSAIR_DEVICE_COUNT_MAX, 
            ctypes.cast(devices_array, ctypes.c_void_p),  # Cast array to void*
            ctypes.byref(size)
        )

        # Helpful debug output
        LOG.debug("CorsairGetDevices returned: %d", res)
        LOG.debug("Devices filled count (size.value): %d", size.value)

        if res != CE_Success:
            if res == CE_ServerNotFound:
                raise CUEError("CorsairGetDevices failed: iCUE server not found. Make sure iCUE is running.")
            elif res == CE_InvalidArguments:
                raise CUEError("CorsairGetDevices failed: Invalid arguments")
            else:
                raise CUEError(f"CorsairGetDevices failed (error {res})")

        out: List[DeviceInfo] = []
        for i in range(size.value):
            d = devices_array[i]
            
            # All fields are char arrays (UTF-8 encoded)
            # Extract null-terminated strings
            device_id = bytes(d.id).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            serial = bytes(d.serial).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            model = bytes(d.model).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            
            LOG.debug("Device %d: id=%s model=%s serial=%s", i, device_id, model, serial)
            out.append(DeviceInfo(idx=i,
                                device_type=int(d.type),
                                device_id=device_id,
                                serial=serial,
                                model=model,
                                led_count=int(d.ledCount),
                                channel_count=int(d.channelCount)))
        return out

    def read_battery_property(self, device_id: str) -> Optional[int]:
        """If device exposes CDPI_BatteryLevel (CDPI_BatteryLevel == 9), read and return 0..100 integer."""
        
        # Convert device_id to bytes for C API
        device_id_bytes = device_id.encode('utf-8')
        
        LOG.debug("Attempting to read battery for device: %s", device_id)
        
        # First, verify the device exists by calling GetDeviceInfo
        test_info = CorsairDeviceInfoStruct()
        verify_res = self.dll.CorsairGetDeviceInfo(device_id_bytes, ctypes.byref(test_info))
        if verify_res != CE_Success:
            LOG.debug("CorsairGetDeviceInfo verification failed with error %d - device ID may be invalid", verify_res)
            return None
        LOG.debug("Device verification succeeded: model=%s", bytes(test_info.model).split(b"\x00", 1)[0].decode("utf-8", errors="ignore"))
        
        dtype = ctypes.c_int()
        flags = ctypes.c_uint()
        res = self.dll.CorsairGetDevicePropertyInfo(device_id_bytes, CDPI_BatteryLevel, 0, ctypes.byref(dtype), ctypes.byref(flags))
        
        # Map error codes to names for better logging
        error_names = {
            CE_Success: "Success",
            CE_ServerNotFound: "ServerNotFound", 
            CE_NoControl: "NoControl",
            CE_IncompatibleProtocol: "IncompatibleProtocol",
            CE_InvalidArguments: "InvalidArguments",
            CE_InvalidOperation: "InvalidOperation",
            CE_DeviceNotFound: "DeviceNotFound",
            CE_NotAllowed: "NotAllowed"
        }
        
        if res != CE_Success:
            error_name = error_names.get(res, f"Unknown({res})")
            LOG.debug("CorsairGetDevicePropertyInfo returned %s (%d) for device %s - battery property may not be supported", 
                     error_name, res, device_id)
            
            # If it's InvalidOperation, the device might not support battery property
            # This is normal for wired devices or devices without battery reporting
            if res == CE_InvalidOperation:
                LOG.debug("Device does not have battery property (likely wired or doesn't report battery)")
            
            return None
            
        # Ensure readable
        CPF_CanRead = 0x01
        if not (flags.value & CPF_CanRead):
            LOG.debug("Battery property exists but not readable (flags=0x%x)", flags.value)
            return None

        LOG.debug("Battery property found, type=%d, flags=0x%x - attempting to read...", dtype.value, flags.value)

        # Allocate a CorsairProperty struct (not a pointer to pointer!)
        prop = CorsairPropertyStruct()
        LOG.debug("Calling CorsairReadDeviceProperty...")
        
        try:
            res = self.dll.CorsairReadDeviceProperty(device_id_bytes, CDPI_BatteryLevel, 0, ctypes.byref(prop))
            LOG.debug("CorsairReadDeviceProperty returned: %d", res)
        except Exception as e:
            LOG.error("CorsairReadDeviceProperty threw exception: %s", e)
            return None
            
        if res != CE_Success:
            error_name = error_names.get(res, f"Unknown({res})")
            LOG.debug("CorsairReadDeviceProperty failed: %s (%d)", error_name, res)
            return None
        
        LOG.debug("Property read successfully, type=%d", prop.type)
            
        try:
            if prop.type == CT_Int32:
                # int32 is stored in union int32
                raw = int(prop.value.int32)
                LOG.debug("Battery level (int32): %d", raw)
                # clamp & return
                return max(0, min(100, raw))
            elif prop.type == CT_String:
                # parse ascii string representation
                s = ctypes.cast(prop.value.string, ctypes.c_char_p).value
                if s is None:
                    return None
                try:
                    parsed = int(s.decode() if isinstance(s, bytes) else s)
                    LOG.debug("Battery level (string): %d", parsed)
                    return max(0, min(100, parsed))
                except Exception:
                    return None
            else:
                LOG.debug("Unhandled CorsairDataType for battery: %s", prop.type)
                return None
        except Exception as e:
            LOG.error("Error reading property value: %s", e)
            return None


# ---- CLI / main ----
def parse_args():
    p = argparse.ArgumentParser(description="Read Corsair headset battery using the iCUE SDK")
    p.add_argument("--device-index", type=int, default=None, help="If specified, target this device index from the enumerated list")
    p.add_argument("--poll", type=float, default=None, help="If specified, poll every N seconds (Ctrl-C to stop)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    
    LOG.info("Python bitness: %s", platform.architecture()[0])
    
    try:
        dll = load_cue_dll()
        LOG.info("Successfully loaded iCUE SDK DLL")
    except Exception as e:
        LOG.error("Failed to load DLL: %s", e)
        sys.exit(1)

    client = CueSdkClient(dll)
    try:
        client.connect()
    except Exception as e:
        LOG.error("Failed to connect to iCUE SDK: %s", e)
        sys.exit(2)

    try:
        devices = client.get_devices()
        if not devices:
            LOG.info("No Corsair devices found via SDK.")
            return
        # Choose device
        if args.device_index is not None:
            if args.device_index < 0 or args.device_index >= len(devices):
                LOG.error("Invalid device-index %s (0..%s)", args.device_index, len(devices)-1)
                return
            devices = [devices[args.device_index]]
        # Print device list summary
        LOG.info("Detected devices:")
        for d in devices:
            LOG.info("[%d] id=%s model=%s serial=%s leds=%d channels=%d type=%d",
                     d.idx, d.device_id, d.model, d.serial, d.led_count, d.channel_count, d.device_type)

        def read_all_once():
            for d in devices:
                val = client.read_battery_property(d.device_id)
                if val is None:
                    print(f"[{d.idx}] {d.model} ({d.device_id}): battery property not available")
                else:
                    print(f"[{d.idx}] {d.model} ({d.device_id}): battery = {val}%")

        if args.poll is None:
            read_all_once()
        else:
            try:
                while True:
                    read_all_once()
                    time.sleep(float(args.poll))
            except KeyboardInterrupt:
                LOG.info("Stopping polling (Ctrl-C)")

    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()