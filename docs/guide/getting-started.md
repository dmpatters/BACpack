# Getting started

From a fresh Windows install to the sample building's four devices in your BACnet tool. On Linux only step 1 differs: see the [Linux install](../../README.md#linux).

1. Install. Run `BACpack-<version>-setup.exe`, accept the port (8000), choose to create the login on the first visit and finish. The setup is unsigned, so Windows shows "Windows protected your PC": choose **More info**, then **Run anyway**. The zip and other options are in the [Windows install](../../README.md#windows).
2. Create a login. Open `https://localhost:8000` (from another computer, `https://<host>:8000`; until a login exists, use an IP address, the computer's name, `name.local` or `localhost`: another name gets "misdirected request") and accept the certificate warning once. Choose **Create login**, enter a username and a password of at least 12 characters, and click **Create login**. Keep the login on: [SECURITY.md](../../SECURITY.md) says why. A forgotten password is reset with `bacpack-admin.cmd reset-login`, from an elevated prompt in `C:\Program Files\BACpack`.
3. On **Network Settings**, set **Default bind address** to this computer's address on your BACnet network, with prefix length (`192.168.1.50/24`). Click **Save network settings**.
4. If your BACnet tool runs on this computer, open its BACnet/IP channel on that address now so it holds UDP 47808. The sample puts its devices on other ports.
5. On the **Devices** page click **Load sample building**, then **Load**.
6. Find the devices.
   - Tool on another computer: allow the VAVs' UDP ports on the BACpack computer's firewall ([the commands](sample-building.md#firewall)), then send a Who-Is on 47808. All four devices answer.
   - Tool on this computer: register it as a foreign device with Sample AHU-1 at the address and port the load panel showed ([how](sharing-the-bacnet-port.md#3-another-port-foreign-device-registration)), then send a Who-Is.

If Windows asks whether to let your tool use the network, allow it.

Next: [a tour of the web UI](web-ui-tour.md), an [EDE file](importing-an-ede-file.md) or a [live controller](cloning-a-live-controller.md).
