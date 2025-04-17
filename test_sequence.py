import OpenImageIO as oiio
import numpy as np
import os

# Create output directory if it doesn't exist
output_dir = "test_sequence"
if not os.path.exists(output_dir):
    os.makedirs(output_dir)

# Create a sequence of 10 test images
for i in range(1, 11):
    # Create a new image buffer
    spec = oiio.ImageSpec(640, 480, 3, oiio.TypeDesc(oiio.FLOAT))
    buf = oiio.ImageBuf(spec)

    # Fill with random data
    pixels = np.random.rand(480, 640, 3).astype(np.float32)
    # Add frame number as a gradient
    pixels[:, :, 0] += i / 10.0  # Add increasing red tint per frame
    buf.set_pixels(oiio.ROI(), pixels)

    # Write to disk
    filename = os.path.join(output_dir, f"test.{i:04d}.exr")
    buf.write(filename)
    print(f"Wrote {filename}")

print("Successfully created test sequence") 