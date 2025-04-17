import OpenImageIO as oiio
import numpy as np

# Create a new image buffer
spec = oiio.ImageSpec(64, 64, 3, oiio.TypeDesc(oiio.FLOAT))
buf = oiio.ImageBuf(spec)

# Fill with random data
pixels = np.random.rand(64, 64, 3).astype(np.float32)
buf.set_pixels(oiio.ROI(), pixels)

# Write to disk
buf.write('test.jpg')
print("Successfully wrote test.jpg")

# Read it back
read_buf = oiio.ImageBuf('test.jpg')
print(f"Read back image dimensions: {read_buf.spec().width}x{read_buf.spec().height}x{read_buf.spec().nchannels}") 