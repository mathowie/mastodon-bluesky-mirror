"""Small image-library smoke test shared by Docker builds and runtime health."""
from io import BytesIO
import json

REPAIR_MESSAGE = (
    'Image processing is unavailable in this container. '
    'Run rebuild.sh on the NAS to rebuild the image and recreate the container.'
)


def image_status():
    try:
        from PIL import Image, __version__
        # Importing PIL alone does not prove its native codecs work.
        with Image.new('RGB', (2, 2), (64, 128, 192)) as image:
            for format_name in ('JPEG', 'PNG'):
                output = BytesIO()
                image.save(output, format=format_name)
                with Image.open(BytesIO(output.getvalue())) as decoded:
                    decoded.load()
                    if decoded.size != (2, 2):
                        raise ValueError('Image codec smoke test failed')
    except (ImportError, OSError, RuntimeError, ValueError):
        return {'ready': False, 'pillow_version': None, 'error': REPAIR_MESSAGE}
    return {'ready': True, 'pillow_version': __version__, 'error': None}


if __name__ == '__main__':
    status = image_status()
    print(json.dumps(status))
    raise SystemExit(0 if status['ready'] else 1)
