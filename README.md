# Yeelight Web Controller
A local web interface for managing Yeelight lighting systems over Wi-Fi, designed to integrate seamlessly with localized smart home infrastructures. This API facilitates the automated modulation of luminous intensity and color temperature.

## System Architecture
The system isolates device communication from API delivery to ensure non-blocking operations:

* Local Network Discovery: The application utilizes multicast protocols to detect active Yeelight hardware on the local network.

* State Caching: The server maintains a persistent local JSON cache. This state-retention mechanism ensures seamless API responses and device tracking, even during intermittent network disconnections.

* HTTP Setup: A FastAPI-based middleware that translates standard HTTP GET requests into native Yeelight LAN control commands.

## Limitations
Currently, this web controller exclusively supports Yeelight hardware models equipped with both variable luminance (dimming) and adjustable color temperature capabilities.

## Requirements
* Python 3.12 or higher.

* Lighting devices must have **LAN Control** explicitly enabled via the **Yeelight Classic** application prior to integration.

## Installation
* Create a virtual environment and install the package using your preferred Python package manager.

  ```bash
    python -m venv venv
    source venv/bin/activate
    pip install yeelight-web-controller
  ```

## Execution
* Start the service by executing the primary command. The server will initialize and begin caching device states.

  ```bash
    yeelight-srv -p 9800
  ```

Note: You can pass -p or --port to specify a custom web service port (default is 9800).

## API Endpoints
### Acquire device inventory

* URL: `/api/lights`

* Method: GET

* Description: Retrieves the consolidated list of discovered and cached devices. The payload includes the IP address, hardware ID, model identifier, current color temperature (Kelvin), and brightness percentage.

* Example: ```http://localhost:9800/api/lights```

* Response Structure:

  ```JSON
  {
    "status": "success",
    "data": [
      {
        "ip": "192.168.1.100",
        "id": "0x000000002ce4355f",
        "model": "color",
        "temperature_k": 4800,
        "brightness_pct": 80
      },
      {
        "ip": "192.168.1.101",
        "id": "0x000000002ce4356a",
        "model": "mono",
        "temperature_k": 0,
        "brightness_pct": 0
      }
    ]
  }
  ```

### Configure light state

* URL: `/api/set`

* Method: GET

* Description: Modifies the operational state of a specific lighting unit. All successful state changes execute with a standardized 1000ms transition duration.

* Parameters:

  - `id` (string, required): The specific hardware ID of the device.
  
  - `brightness` (float, required): The target luminance parameter.
  
    - Value <= 0.0: Triggers device power off.
  
    - Value > 0.0 and < 1.0: Triggers Moonlight Mode if supported by the hardware model.
  
    - Value >= 1.0: Triggers Normal Mode.
  
  - `temp` (integer, optional): The target color temperature in Kelvin. Applicable primarily in Normal Mode.

* Example: ```http://localhost:9800/api/set?id=0x000000002ce4355f&brightness=0.8&temp=4800```

* Response Structure:
  ```json
  {
    "status": "success",
    "message": "Operation successful"
  }
  ```

* Note: In the event of an invalid parameter or unlocated device, the system will return an HTTP 400 or 404 status code alongside an error payload, such as:

  ```json
  {
    "status": "error",
    "message": "Device not found"
  }
  ```

### Assign device alias
  
* URL: `/api/name`

* Method: POST

* Description: Assigns or updates a persistent, human-readable identifier for a specific hardware ID. This nomenclature is committed to the local `names.json` cache.

* Payload Schema:
  ```json
  {
    "bulb_id": "0x000000002ce4355f",
    "name": "Desk Lamp"
  }
  ```

* Example Request
  ```bash
  curl -X POST http://localhost:9800/api/name \
  -H "Content-Type: application/json" \
  -d '{"bulb_id": "0x000000002ce4355f", "name": "Desk Lamp"}'
  ```

* Response Structure
  ```json
  {
    "status": "success",
    "message": "Device name updated successfully"
  }
  ```

### Matter Logical Level Control

* URL: `/api/level`

* Method: GET

* Description: Retrieves or modifies the brightness level mapping to the Matter-compatible logical range (0-254).

* Parameters:

  - `id` (string, required): The specific hardware ID of the device.

  - `level` (integer, optional): The target logical brightness level (0-254). If omitted, returns the current device level.

### Color Temperature Control (Kelvin)

* URL: /api/kelvin

* Method: GET

* Description: Retrieves or modifies the exact color temperature in Kelvin.

* Parameters:

  - `id` (string, required): The specific hardware ID of the device.

  - `kelvin` (integer, optional): The target color temperature in Kelvin. If omitted, returns the current Kelvin value.

### Color Temperature Control (Mired)

* URL: `/api/mired`

* Method: GET

* Description: Retrieves or modifies the color temperature using the Matter-compatible Mired (Micro Reciprocal Degree) range.

* Parameters:

  - `id` (string, required): The specific hardware ID of the device.

  - `mired` (integer, optional): The target color temperature in Mired. If omitted, returns the current Mired value.

### Retrieve Bridge Metadata

* URL: `/api/metadata`

* Method: GET

* Description: Retrieves dynamic bridge configuration and device metadata required for Matter ecosystem integration.