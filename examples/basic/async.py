import asyncio

from rascube_v2 import AsyncRASCube, prompt_connection


async def main(port: str, serial_number: int) -> None:
    async with AsyncRASCube(port, serial_number=serial_number) as cube:
        receiver = await cube.receiver.get_info()
        print("Receiver:", receiver)

        obc = await cube.obc.get_info()
        print("OBC:", obc)

        presence = await cube.addons.refresh_enabled()
        print("Enabled add-ons:", sorted(presence.enabled_ids))

        async with cube.telemetry.samples.subscribe() as samples:
            async for sample in samples:
                print(sample.device_uptime_ms, sample.gps.latitude, sample.gps.longitude)


if __name__ == "__main__":
    port, serial_number = prompt_connection()
    asyncio.run(main(port, serial_number))
