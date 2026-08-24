from contextlib import suppress
from io import BytesIO
from queue import Empty, Queue
from threading import Thread
from time import monotonic, sleep

import matplotlib.pyplot as plt
from PIL import Image, ImageFile

from rascube_v2 import SyncRASCube, prompt_connection
from rascube_v2.models.camera import CameraBlock, CameraImage

CAPTURE_TIMEOUT_SECONDS = 30.0
PREVIEW_INTERVAL_SECONDS = 0.1
JPEG_EOI = b"\xff\xd9"

ImageFile.LOAD_TRUNCATED_IMAGES = True


def contiguous_jpeg(blocks: dict[int, bytes]) -> bytes | None:
    if 0 not in blocks:
        return None
    data = bytearray()
    index = 0
    while index in blocks:
        data.extend(blocks[index])
        index += 1
    if not data.startswith(b"\xff\xd8"):
        return None
    eoi = data.find(JPEG_EOI)
    return bytes(data if eoi < 0 else data[: eoi + 2])


def decode_partial_jpeg(jpeg: bytes) -> Image.Image | None:
    if not jpeg.endswith(JPEG_EOI):
        jpeg += JPEG_EOI
    try:
        with Image.open(BytesIO(jpeg)) as image:
            image.load()
            return image.convert("RGB")
    except OSError:
        return None


def main(port: str, serial_number: int) -> None:
    received_blocks: dict[int, bytes] = {}
    block_queue: Queue[CameraBlock] = Queue()
    result_queue: Queue[CameraImage | BaseException] = Queue(maxsize=1)

    plt.ion()
    figure, axes = plt.subplots()
    axes.axis("off")
    status = axes.set_title("Waiting for camera data...")
    image_artist = None
    figure.tight_layout()
    figure.show()

    def render(jpeg: bytes, title: str) -> bool:
        nonlocal image_artist
        pixels = decode_partial_jpeg(jpeg)
        if pixels is None:
            return False
        if image_artist is None:
            image_artist = axes.imshow(pixels)
        else:
            image_artist.set_data(pixels)
        status.set_text(title)
        figure.canvas.draw_idle()
        return True

    with SyncRASCube(port, serial_number=serial_number) as cube:

        def capture() -> None:
            try:
                result_queue.put(
                    cube.camera.capture(
                        timeout=CAPTURE_TIMEOUT_SECONDS,
                        on_block=block_queue.put,
                    )
                )
            except BaseException as exc:
                result_queue.put(exc)

        capture_thread = Thread(target=capture, name="camera-capture")
        capture_thread.start()
        last_preview = 0.0
        preview_pending = False

        while capture_thread.is_alive() or not block_queue.empty():
            while True:
                try:
                    block = block_queue.get_nowait()
                except Empty:
                    break
                received_blocks[block.index] = block.data
                preview_pending = True

            now = monotonic()
            if (
                preview_pending
                and now - last_preview >= PREVIEW_INTERVAL_SECONDS
                and plt.fignum_exists(figure.number)
            ):
                partial = contiguous_jpeg(received_blocks)
                if partial is not None:
                    render(partial, f"Streaming camera: {len(received_blocks)} blocks")
                last_preview = now
                preview_pending = False

            if plt.fignum_exists(figure.number):
                figure.canvas.flush_events()
            sleep(0.02)

        capture_thread.join()
        result = result_queue.get()
        if isinstance(result, BaseException):
            raise result
        image = result

    duplicate_count = len(image.duplicate_blocks)
    print(
        f"Captured {len(image.jpeg)} JPEG bytes in {image.block_count} blocks "
        f"({duplicate_count} duplicates)"
    )
    if plt.fignum_exists(figure.number):
        render(
            image.jpeg,
            f"Capture complete: {image.block_count} blocks, {duplicate_count} duplicates",
        )
        figure.canvas.flush_events()
        plt.ioff()
        plt.show()


if __name__ == "__main__":
    selected_port, selected_serial_number = prompt_connection()
    with suppress(KeyboardInterrupt):
        main(selected_port, selected_serial_number)
