#!/usr/bin/env python3
from __future__ import with_statement
from __future__ import print_function
from __future__ import division

import os, sys, re
from glob import glob
import time, datetime
import logging
import argparse, shlex
import subprocess

from tc import Timecode
import pyseq
try:
    import OpenImageIO as oiio
    import numpy as np
    import yaml
    from PIL import Image
except ImportError:
    print("Error: Missing dependencies. Need:\n\tOpenImageIO\n\tNumPy\n\tPyYAML\n\tPillow (for mjpeg codec conversion)")


"""
    Daily
    ---------------------
    This is a program to render a dailies movie from an input image sequence (jpegs or exrs).
    It reads from a configuration file to define things like resize, color transforms, padding,
    text overalys, slate frames and so forth.

"""
"""
    Commandline python program to take an openexr image sequence, apply an ocio display transform, resize,
    and output to sdtout as raw uint16 byte data.
    Inputs:
        image sequence in /path/to/imagename.%05d.exr format
        optional: framerange to use starframe-endframe
        ocio display and ocio view to apply.
        ocio config to use
        resize width
        resize pad to fit (optional)
    Example Command:
    ./exrpipe -i '/mnt/cave/dev/__pipeline-tools/generate_dailies/test_footage/monkey_test/M07-2031.%05d.exr' -s 190 -e 200 -d ACES -v RRT -r 2048x1152 | ffmpeg-10bit -f rawvideo -pixel_format rgb48le -video_size 1920x1080 -framerate 24 -i pipe:0
    -c:v libx264 -profile:v high444 -preset veryslow -g 1 -tune film -crf 13 -pix_fmt yuv444p10le -vf "colormatrix=bt601:bt709" test.mov
"""
"""
This is an example of Google style.

Args:
    param1: This is the first param.
    param2: This is a second param.

Returns:
    This is a description of what is returned.

Raises:
    KeyError: Raises an exception.

TODO
    - Generalized metadata access
    - Text from metadata information
    - Add crop config and better textelement config options to the nuke setup tool
    - Refactor to calculate format only once for image sequences?
    - Revise logging:
        - clean up log for each frame.
        - print fps for each frame
        - print total calculation time at end: summary of frames calculated, time per frame average fps etc
"""

dir_path = os.path.dirname(os.path.realpath(__file__))

DAILIES_CONFIG_DEFAULT = os.path.join(dir_path, "dailies-config.yaml")
DEFAULT_CODEC = 'avchq'
DEFAULT_DAILIES_PROFILE = 'delivery'

DEBUG = False

log = logging.getLogger(__name__)


class GenerateDaily():

    def __init__(self):
        """
        Initial setup: gather and validate config and input data.
        Args:
            None
        Returns:
            Nothing is returned. If self.setup_success = is True, it is ready to process()
        """

        self.start_time = time.time()
        self.setup_success = False


        # Parse Config File
        DAILIES_CONFIG = os.getenv("DAILIES_CONFIG")
        if not DAILIES_CONFIG:
            DAILIES_CONFIG = DAILIES_CONFIG_DEFAULT

        # Get Config file data
        if os.path.isfile(DAILIES_CONFIG):
            with open(DAILIES_CONFIG, 'r') as configfile:
                config = yaml.safe_load(configfile)
        else:
            print("Error: Could not find config file {0}".format(DAILIES_CONFIG))
            self.setup_success = False
            return

        # Get list of possible output profiles from config.
        output_codecs = config.get("output_codecs").keys()

        # Get list of dailies profiles
        dailies_profiles = config.get("dailies_profiles")
        ocio_profiles = config.get("ocio_profiles")

        # Parse input arguments
        parser = argparse.ArgumentParser(description='Process given image sequence with ocio display, resize and output to ffmpeg for encoding into a dailies movie.')
        parser.add_argument("input_path", help="Input exr image sequence. Can be a folder containing images, a path to the first image, a percent 05d path, or a ##### path.")
        parser.add_argument("-c", "--codec", help="Codec name: Possible options are defined in the DAILIES_CONFIG:\n{0}".format("\n\t".join(output_codecs)))
        parser.add_argument("-p", "--profile", help="Dailies profile: Choose the settings to use for dailies overlays:\n{0}".format("\n\t".join(dailies_profiles.keys())))
        parser.add_argument("-o", "--output", help="Output directory: Optional override to movie_location in the DAILIES_CONFIG. This can be a path relative to the image sequence.")
        parser.add_argument("-t", '--text', help="Text elements and contents to add: e.g. \n\t\"artist: Jed Smith | comment: this is stupid man|")
        parser.add_argument("-ct", "--color_transform", help="OCIO Colorspace Conversion preset to use. Specified in the dailies config under ocio_profiles.\n{0}".format(" ".join(ocio_profiles.keys())))
        parser.add_argument("--ocio", help="OCIO Colorspace Conversion to use. Specified in the dailies config under ocio_profiles.\n{0}".format(" ".join(ocio_profiles.keys())))
        parser.add_argument("-d", "--debug", help="Set debug to true.", action="store_true")

        # Show help if no args.
        if len(sys.argv)==1:
            parser.print_help()
            return None

        args = parser.parse_args()

        input_path = args.input_path
        codec = args.codec
        input_dailies_profile = args.profile
        self.movie_location = args.output
        texts = args.text
        commandline_ocio_profile = args.color_transform
        commandline_ocio_profile = args.ocio
        if args.debug:
            print("Setting DEBUG=True!")
            DEBUG = True

        if texts:
            texts = texts.split('|')
        else:
            texts = []

        # Assemble text elements contents dict
        self.text = {}
        for text in texts:
            key, value = text.split(':')
            key = key.strip()
            value = value.strip()
            self.text[key] = value

        # Use current directory if no input path specified
        if not input_path:
            input_path = os.getcwd()


        # Get Config dicts for globals and the "codec" config from the config file
        self.globals_config = config.get("globals")


        # Use default output codec from config if none specified.
        if not codec:
            config_default_codec = self.globals_config.get('output_codec')
            if config_default_codec:
                codec = config_default_codec
            else:
                codec = DEFAULT_CODEC
        if codec not in output_codecs:
            print("Error: invalid codec specified. Possible options are \n\t{0}".format("\n\t".join(output_codecs)))
            self.setup_success = False
            return

        self.codec_config = config["output_codecs"][codec]

        # Gather image sequences from input path
        self.image_sequences = self.get_image_sequences(input_path)
        if not self.image_sequences:
            print("No image sequence found! Exiting...")
            self.setup_success = False
            return


        # Get dailies profile config
        if not input_dailies_profile:
            input_dailies_profile = DEFAULT_DAILIES_PROFILE

        self.profile_config = config.get("dailies_profiles")[input_dailies_profile]


        # Add datetime
        if self.profile_config.get('text_elements'):
            datetime_format_string = self.profile_config.get('text_elements').get('datetime').get('datetime_format')
        else:
            datetime_format_string = None
        if datetime_format_string:
            self.text["datetime"] = datetime.datetime.now().strftime(datetime_format_string)
        else:
            self.text['datetime'] = datetime.datetime.now().replace(microsecond=0).isoformat()


        #############################################################################################################
        # Validate config information.  Directly modifies the self.globals_config and self.codec_config vars
        #############################################################################################################


        #####################################
        # Set up OCIO color transform
        # -----------------------------------

        # Get OCIO config and verify it exists
        self.ocioconfig = self.globals_config.get('ocioconfig')
        if self.ocioconfig:
            log.debug("Got OCIO config from dailies-config: {0}".format(self.ocioconfig))
        # Try to get ocio config from $OCIO env-var if it's not defined
        if not self.ocioconfig:
            env_ocio = os.getenv("OCIO")
            if env_ocio:
                self.globals_config['ocioconfig'] = env_ocio
                self.ocioconfig = env_ocio

        if not os.path.exists(self.ocioconfig):
            log.warning("OCIO Config does not exist: \n\t{0}\n\tNo OCIO color transform will be applied".format(
                self.ocioconfig))
            self.ocioconfig = None

        # Get default ocio transform to use if none is passed by commandline
        ocio_default_transform = self.globals_config.get("ocio_default_transform")
        ocio_default_colorspace = self.globals_config.get("ocio_default_colorspace")

        # Set self.ociocolorconvert: the colorspace transformation to use in processing
        if commandline_ocio_profile:
            # Check if specified ocio profile exists in the config
            if commandline_ocio_profile in ocio_profiles.keys():
                self.ociocolorconvert = ocio_profiles.get(commandline_ocio_profile).get('ociocolorconvert')
                print("\nColor Space Information:")
                print(f"Using command line specified OCIO profile: {commandline_ocio_profile}")
                print(f"Source colorspace: {self.ociocolorconvert[0]}")
                print(f"Destination colorspace: {self.ociocolorconvert[1]}")
            else:
                print(f"\nError: OCIO color transform {commandline_ocio_profile} does not exist in config.")
                if ocio_default_transform in ocio_profiles.keys():
                    print(f"Falling back to default transform: {ocio_default_transform}")
                    self.ociocolorconvert = ocio_profiles.get(ocio_default_transform).get('ociocolorconvert')
                    print(f"Source colorspace: {self.ociocolorconvert[0]}")
                    print(f"Destination colorspace: {self.ociocolorconvert[1]}")
                else:
                    print(f"Error: Default OCIO transform {ocio_default_transform} also not found in config.")
                    self.ociocolorconvert = None
        elif ocio_default_transform:
            # Check if default ocio transform exists in the config
            if ocio_default_transform in ocio_profiles.keys():
                self.ociocolorconvert = ocio_profiles.get(ocio_default_transform).get('ociocolorconvert')
                print("\nColor Space Information:")
                print(f"Using default OCIO profile: {ocio_default_transform}")
                print(f"Source colorspace: {self.ociocolorconvert[0]}")
                print(f"Destination colorspace: {self.ociocolorconvert[1]}")
            else:
                print(f"\nError: OCIO color transform {ocio_default_transform} does not exist in config.")
                self.ociocolorconvert = None
        else:
            # No ocio color transform specified
            print("\nWarning: No OCIO color transform specified.")
            print("No color transform will be applied.")
            self.ociocolorconvert = None

        # Print OCIO config path
        print(f"\nOCIO Config Path: {self.ocioconfig}")
        if not os.path.exists(self.ocioconfig):
            print("Warning: OCIO Config file not found at specified path!")

        # Set up view transform if specified
        self.ocioview = None
        if self.globals_config.get('ocio_view'):
            self.ocioview = self.globals_config.get('ocio_view')
            print(f"Using view transform: {self.ocioview}")

        # Set up display if specified
        self.ociodisplay = None
        if self.globals_config.get('ocio_display'):
            self.ociodisplay = self.globals_config.get('ocio_display')
            print(f"Using display: {self.ociodisplay}")

        print("\n=== Moving to OIIO Image Processing ===")

        # Anything with the same name in the codec config overrides the globals
        for key, value in self.codec_config.items():
            if key in self.globals_config:
                if self.codec_config[key]:
                    self.globals_config[key] = value

        # Get output width and height
        self.output_width = self.globals_config['width']
        self.output_height = self.globals_config['height']

        # If output width or height is not defined, we need to calculate it from the input images
        if not self.output_width or not self.output_height:
            buf = oiio.ImageBuf(self.image_sequence[0].path)
            spec = buf.spec()
            iar = float(spec.width) / float(spec.height)
            if not self.output_width:
                self.output_width = spec.width
                self.globals_config['width'] = self.output_width
            if not self.output_height:
                self.output_height = int(round(self.output_width / iar))
                self.globals_config['height'] = self.output_height
            # buf.close()

        self.setup_success = True


        if self.setup_success == True:
            for self.image_sequence in self.image_sequences:
                self.process()



    def process(self):
        """
        Performs the actual processing of the movie.
        Args:
            None
        Returns:
            None
        """


        # Set up movie file location and naming

        # Crop separating character from sequence basename if there is one.
        seq_basename = self.image_sequence.head()

        if seq_basename.endswith(self.image_sequence.parts[-2]):
            seq_basename = seq_basename[:-1]

        movie_ext = self.globals_config['movie_ext']


        # Handle relative / absolute paths for movie location
        # use globals config for movie location if none specified on the commandline
        if not self.movie_location:
            self.movie_location = self.globals_config['movie_location']
            print("No output folder specified. Using Output folder from globals: {0}".format(self.movie_location))

        # Convert forward slashes to backslashes for Windows
        self.movie_location = self.movie_location.replace('/', '\\')
        
        # Create the output directory if it doesn't exist
        os.makedirs(self.movie_location, exist_ok=True)

        # Create the full output path
        if self.globals_config['movie_append_codec']:
            codec_name = self.codec_config.get('name', '')
            if codec_name:
                movie_basename = seq_basename + "_" + codec_name
            else:
                movie_basename = seq_basename
        else:
            movie_basename = seq_basename

        movie_filename = movie_basename + "." + self.globals_config['movie_ext']
        self.movie_fullpath = os.path.join(self.movie_location, movie_filename)

        # Ensure the output directory exists
        os.makedirs(os.path.dirname(self.movie_fullpath), exist_ok=True)

        # Print the final output path for verification
        print("Output path: {0}".format(self.movie_fullpath))

        # Set up Logger
        log_fullpath = os.path.splitext(self.movie_fullpath)[0] + ".log"
        if os.path.exists(log_fullpath):
            os.remove(log_fullpath)
        handler = logging.FileHandler(log_fullpath)
        handler.setFormatter(
            logging.Formatter('%(levelname)s\t %(asctime)s \t%(message)s', '%Y-%m-%dT%H:%M:%S')
            )
        log.addHandler(handler)
        if self.globals_config['debug']:
            log.setLevel(logging.DEBUG)
        else:
            log.setLevel(logging.INFO)
        log.debug("Got config:\n\tCodec Config:\t{0}\n\tImage Sequence Path:\n\t\t{1}".format(
            self.codec_config['name'], self.image_sequence.path()))

        log.debug("Output width x height: {0}x{1}".format(self.output_width, self.output_height))

        # Set pixel_data_type based on config bitdepth
        if self.codec_config['bitdepth'] > 8:
            self.pixel_data_type = oiio.UINT16
        else:
            self.pixel_data_type = oiio.UINT8


        # Get timecode based on frame
        tc = Timecode(self.globals_config['framerate'], start_timecode='00:00:00:00')
        self.start_tc = tc + self.image_sequence.start()

        # Set up ffmpeg command
        ffmpeg_args = self.setup_ffmpeg()

        log.info("ffmpeg command:\n\t{0}".format(ffmpeg_args))


        # Static image buffer for text that doesn't change frame to frame
        self.static_text_buf = oiio.ImageBuf(oiio.ImageSpec(self.output_width, self.output_height, 4, self.pixel_data_type))

        # Loop through each text element, create the text image, and add it to self.static_text_buf
        text_elements = self.profile_config.get('text_elements')
        if text_elements:
            for text_element_name, text_element in text_elements.items():
                self.generate_text(text_element_name, text_element, self.static_text_buf)


        if not DEBUG:
            # Invoke ffmpeg subprocess
            ffproc = subprocess.Popen(
                shlex.split(ffmpeg_args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE
                )

        # Loop through every frame, passing the result to the ffmpeg subprocess
        for i, self.frame in enumerate(self.image_sequence, 1):

            log.info("Processing frame {0:04d}: \t{1:04d} of {2:04d}".format(self.frame.frame, i, self.image_sequence.length()))
            # elapsed_time = datetime.timedelta(seconds = time.time() - start_time)
            # log.info("Time Elapsed: \t{0}".format(elapsed_time))
            frame_start_time = time.time()

            buf = self.process_frame(self.frame)

            # Set framecounter in text elements, add framecounter text
            if self.text:
                if self.text.get('framecounter'):
                    self.text['framecounter'] = str(self.frame.frame).zfill(text_elements.get('framecounter').get('padding'))
                    buf = self.generate_text('framecounter', text_elements.get('framecounter'), buf)


            if not DEBUG:
                # If MJPEG: convert from raw byte data to jpeg before passing to ffmpeg for concatenation
                pixels = buf.get_pixels(self.pixel_data_type)
                if self.codec_config['name'] == 'mjpeg':
                    jpeg_img = Image.fromarray(pixels)
                    # https://pillow.readthedocs.io/en/5.2.x/handbook/image-file-formats.html#jpeg
                    jpeg_img.save(ffproc.stdin, "JPEG", subsampling="4:4:4", quality=90)
                else:
                    ffproc.stdin.write(pixels)
            else:
                buf.write(os.path.splitext(self.movie_fullpath)[0] + ".{0:05d}.jpg".format(self.frame.frame))

            frame_elapsed_time = datetime.timedelta(seconds=time.time() - frame_start_time)
            log.info("Frame Processing Time: \t{0}".format(frame_elapsed_time))

        if not DEBUG:
            result, error = ffproc.communicate()
        elapsed_time = datetime.timedelta(seconds = time.time() - self.start_time)
        log.info("Total Processing Time: \t{0}".format(elapsed_time))




    def get_image_sequences(self, input_path):
        """
        Get list of image sequence objects given a path on disk.

        Args:
            input_path: Input file path. Can be a directory or file or %05d / ### style

        Returns:
            An image sequence object.
        """
        input_path = os.path.realpath(input_path)
        input_image_formats = self.globals_config.get('input_image_formats')
        print('Processing INPUT PATH: {0}'.format(input_path))
        if os.path.isdir(input_path):
            # Find image sequences recursively inside specified directory
            image_sequences = []
            for root, directories, filenames in os.walk(input_path):
                # If there is more than 1 image file in input_path, search this path for file sequences also
                if root == input_path:
                    image_files = [f for f in filenames if os.path.splitext(f)[-1][1:] in input_image_formats]
                    if len(image_files) > 1:
                        image_sequences += pyseq.get_sequences(input_path)
                for directory in directories:
                    image_sequences += pyseq.get_sequences(os.path.join(root, directory))
            if not image_sequences:
                log.error("Could not find any image files recursively in source directory: {0}".format(input_path))
                return None
        elif os.path.isfile(input_path):
            # Assume it's the first frame of the image sequence
            # Try to split off the frame number to get a glob
            image = pyseq.get_sequences(input_path)
            if image:
                image = image[0]
            image_sequences = pyseq.get_sequences(os.path.join(image.dirname, image.name.split(image.parts[-2])[0]) + "*")

        else:
            # Assume this is a %05d or ### image sequence. Use the parent directory if it exists.
            dirname, filename = os.path.split(input_path)
            if os.path.isdir(dirname):
                image_sequences = pyseq.get_sequences(dirname)
            else:
                image_sequences = None

        if image_sequences:
            # Remove image sequences not in list of approved extensions
            if not input_image_formats:
                input_image_formats = ['exr']
            actual_image_sequences = []
            for image_sequence in image_sequences:
                extension = image_sequence.name.split('.')[-1]
                if extension in input_image_formats:
                    actual_image_sequences.append(image_sequence)
            print("Found image sequences: \n{0}".format(image_sequences))
            return actual_image_sequences
        else:
            log.error("Could not find any Image Sequences!!!")
            return None



    def setup_ffmpeg(self):
        """
        Constructs an ffmpeg command based on the given codec config.

        Returns:
            A string containing the entire ffmpeg command to run.
        """

        # ffmpeg-10bit No longer necessary in ffmpeg > 4.1
        ffmpeg_command = "ffmpeg"

        if self.codec_config.get('bitdepth', 8) >= 10:
            pixel_format = "rgb48le"
        else:
            pixel_format = "rgb24"

        if self.codec_config.get('name') == 'mjpeg':
            # Set up input arguments for frame input through pipe:
            args = "{0} -y -framerate {1} -i pipe:0".format(ffmpeg_command, self.globals_config['framerate'])
        else:
            # Set up input arguments for raw video and pipe:
            args = "{0} -hide_banner -loglevel info -y -f rawvideo -pixel_format {1} -video_size {2}x{3} -framerate {4} -i pipe:0".format(
                ffmpeg_command, pixel_format, self.globals_config['width'], self.globals_config['height'], self.globals_config['framerate'])

        # Add timecode so that start frame will display correctly in RV etc
        args += " -timecode {0}".format(self.start_tc)

        if self.codec_config.get('codec'):
            args += " -c:v {0}".format(self.codec_config['codec'])

        if self.codec_config.get('profile'):
            args += " -profile:v {0}".format(self.codec_config['profile'])

        if self.codec_config.get('qscale'):
            args += " -qscale:v {0}".format(self.codec_config['qscale'])

        if self.codec_config.get('preset'):
            args += " -preset {0}".format(self.codec_config['preset'])

        if self.codec_config.get('keyint'):
            args += " -g {0}".format(self.codec_config['keyint'])

        if self.codec_config.get('bframes'):
            args += " -bf {0}".format(self.codec_config['bframes'])

        if self.codec_config.get('tune'):
            args += " -tune {0}".format(self.codec_config['tune'])

        if self.codec_config.get('crf'):
            args += " -crf {0}".format(self.codec_config['crf'])

        if self.codec_config.get('pix_fmt'):
            args += " -pix_fmt {0}".format(self.codec_config['pix_fmt'])

        if self.globals_config['framerate']:
            args += " -r {0}".format(self.globals_config['framerate'])

        if self.codec_config.get('vf'):
            args += " -vf {0}".format(self.codec_config['vf'])

        if self.codec_config.get('vendor'):
            args += " -vendor {0}".format(self.codec_config['vendor'])

        if self.codec_config.get('metadata_s'):
            args += " -metadata:s {0}".format(self.codec_config['metadata_s'])

        if self.codec_config.get('bitrate'):
            args += " -b:v {0}".format(self.codec_config['bitrate'])

        # Finally add the output movie file path with proper escaping
        output_path = self.movie_fullpath.replace('\\', '/')
        args += ' "{0}"'.format(output_path)

        return args





    def process_frame(self, frame):
        """
        Apply all color and reformat / resize operations to input image, then return the imagebuf

        Args:
            frame: pyseq Item object describing the current frame.
            framenumber: the current frame number

        Returns:
            Returns an oiio.ImageBuf object which holds the altered image data.
        """

        # Setup image buffer
        buf = oiio.ImageBuf(frame.path)
        spec = buf.spec()

        # Get Codec Config and gather information
        iwidth = spec.width
        iheight = spec.height
        if float(iheight) != 0:
            iar = float(iwidth) / float(iheight)
        else:
            log.error("Input height is Zero! Skipping frame {0}".format(frame))
            return

        px_filter = self.globals_config.get('filter')
        self.output_width = self.globals_config.get('width')
        self.output_height = self.globals_config.get('height')
        fit = self.globals_config.get('fit')
        cropwidth = self.globals_config.get('cropwidth')
        cropheight = self.globals_config.get('cropheight')

        # Remove alpha channel
        oiio.ImageBufAlgo.channels(buf, buf, (0,1,2))

        # Apply ocio color transform
        buf = self.apply_ocio_transform(buf)

        # Setup for width and height
        if not self.output_width:
            resize = False
        else:
            resize = True
            # If no output height specified, resize keeping aspect ratio, long side = width - calc height
            oheight_noar = int(self.output_width / iar)
            if not self.output_height:
                self.output_height = oheight_noar
            oar = float(self.output_width) / float(self.output_height)


        # Apply cropwidth / cropheight to remove pixels on edges before applying resize
        if cropwidth or cropheight:
            # Handle percentages
            if type(cropwidth) == str:
                if "%" in cropwidth:
                    cropwidth = int(float(cropwidth.split('%')[0])/100*iwidth)
                    log.info("Got crop width percentage: {0}px".format(cropwidth))
            if type(cropheight) == str:
                if "%" in cropheight:
                    cropheight = int(float(cropheight.split('%')[0])/100*iheight)
                    log.info("Got crop height percentage: {0}px".format(cropheight))

            log.debug("Not Yet CROPPED:{0} {1}".format(buf.spec().width, buf.spec().height))

            buf = oiio.ImageBufAlgo.crop(buf, roi=oiio.ROI(int(cropwidth / 2), int(iwidth - cropwidth / 2), int(cropheight / 2), int(iheight - cropheight / 2)))

            # Remove data window of buffer so resize works from cropped region
            buf.set_full(buf.roi.xbegin, buf.roi.xend, buf.roi.ybegin, buf.roi.yend, buf.roi.chbegin, buf.roi.chend)

            log.debug("CROPPED:{0} {1}".format(buf.spec().width, buf.spec().height))

            # Recalculate input resolution and aspect ratio - since it may have changed with crop
            iwidth = buf.spec().width
            iheight = buf.spec().height
            iar = float(iwidth) / float(iheight)
            oheight_noar = int(self.output_width / iar)

            log.debug("iwidth:{0} x iheight:{1} x iar: {2}".format(iwidth, iheight, iar))



        # Apply Resize / Fit
        # If input and output resolution are the same, do nothing
        # If output width is bigger or smaller than input width, first resize without changing input aspect ratio
        # If "fit" is true,
        # If output height is different than input height: transform by the output height - input height / 2 to center,
        # then crop to change the roi to the output res (crop moves upper left corner)

        identical = self.output_width == iwidth and self.output_height == iheight
        resize = not identical and resize


        if resize:
            log.info("Performing Resize: \n\t\t\tinput: {0}x{1} ar{2}\n\t\t\toutput: {3}x{4} ar{5}".format(iwidth, iheight, iar, self.output_width, self.output_height, oar))

            if iwidth != self.output_width:
                # Perform resize, no change in AR
                log.debug("iwidth does not equal output_width: oheight noar: {0}, pxfilter: {1}".format(oheight_noar, px_filter))

                #############
                #
                if px_filter:
                    # (bug): using "lanczos3", 6.0, and upscaling causes artifacts
                    # (bug): dst buf must be assigned or ImageBufAlgo.resize doesn't work
                    buf = oiio.ImageBufAlgo.resize(buf, px_filter, roi=oiio.ROI(0, self.output_width, 0, oheight_noar))
                else:
                    buf = oiio.ImageBufAlgo.resize(buf, roi=oiio.ROI(0, self.output_width, 0, oheight_noar))

            if fit:
                # If fitting is enabled..
                height_diff = self.output_height - oheight_noar
                log.debug("Height difference: {0} {1} {2}".format(height_diff, self.output_height, oheight_noar))

                # If we are cropping to a smaller height we need to transform first then crop
                # If we pad to a taller height, we need to crop first, then transform.
                if self.output_height < oheight_noar:
                    # If we are cropping...
                    buf = self.oiio_transform(buf, 0, height_diff/2)
                    buf = oiio.ImageBufAlgo.crop(buf, roi=oiio.ROI(0, self.output_width, 0, self.output_height))
                elif self.output_height > oheight_noar:
                    # If we are padding...
                    buf = oiio.ImageBufAlgo.crop(buf, roi=oiio.ROI(0, self.output_width, 0, self.output_height))
                    buf = self.oiio_transform(buf, 0, height_diff/2)



        # Apply Cropmask if enabled
        cropmask_config = self.profile_config.get('cropmask')
        if cropmask_config:
            enable_cropmask = cropmask_config.get('enable')
        else:
            enable_cropmask = False
        if enable_cropmask:
            cropmask_ar = cropmask_config.get('aspect')
            cropmask_opacity = cropmask_config.get('opacity')

            if not cropmask_ar or not cropmask_opacity:
                log.error("Cropmask enabled, but no crop specified. Skipping cropmask...")
            else:
                cropmask_height = int(round(self.output_width / cropmask_ar))
                cropmask_bar = int((self.output_height - cropmask_height)/2)
                log.debug("Cropmask height: \t{0} = {1} / {2} = {3} left".format(cropmask_height, self.output_height, cropmask_ar, cropmask_bar))

                cropmask_buf = oiio.ImageBuf(oiio.ImageSpec(self.output_width, self.output_height, 4, self.pixel_data_type))

                # Fill with black, alpha = cropmask opacity
                oiio.ImageBufAlgo.fill(cropmask_buf, (0, 0, 0, cropmask_opacity))

                # Fill center with black
                oiio.ImageBufAlgo.fill(cropmask_buf, (0, 0, 0, 0), oiio.ROI(0, self.output_width, cropmask_bar, self.output_height - cropmask_bar))

                # Merge cropmask and text over image
                oiio.ImageBufAlgo.channels(buf, buf, (0, 1, 2, 1.0))
                buf = oiio.ImageBufAlgo.over(cropmask_buf, buf)
                buf = oiio.ImageBufAlgo.over(self.static_text_buf, buf)
                oiio.ImageBufAlgo.channels(buf, buf, (0,1,2))

        return buf


    def oiio_transform(self, buf, xoffset, yoffset):
        """
        Convenience function to reposition an image.

        Args:
            buf: oiio.ImageBuf object representing the image to be transformed.
            xoffset: X offset in pixels
            yoffset: Y offset in pixels

        Returns:
            Returns the modified oiio.ImageBuf object which holds the altered image data.
        """
        orig_roi = buf.roi
        buf.specmod().x += int(xoffset)
        buf.specmod().y += int(yoffset)
        buf_trans = oiio.ImageBuf()
        oiio.ImageBufAlgo.crop(buf_trans, buf, orig_roi)
        return buf_trans



    def apply_ocio_transform(self, buf):
        """
        Applies an ocio transform specified in the config. Can be a ociodisplay, colorconvert, or look transform
        For now only colorconvert is supported.
        Reads from self.ocioconfig to specify the ocio config to use.
        Reads from self.ociocolorconvert, a two item list. [0] is src, [1] is dst colorspace.

        Args:
            buf: oiio.ImageBuf object representing the image to be transformed.

        Returns:
            Returns the modified oiio.ImageBuf object which holds the altered image data.
        """

        if self.ociocolorconvert:
            log.debug("Applying OCIO Config: \n\t{0}\n\t{1} -> {2}".format(self.ocioconfig, self.ociocolorconvert[0], self.ociocolorconvert[1]))
            success = oiio.ImageBufAlgo.colorconvert(buf, buf, self.ociocolorconvert[0], self.ociocolorconvert[1], colorconfig=self.ocioconfig)
            if not success:
                log.error("Error: OCIO Color Convert failed. Please check that you have the specified colorspaces in your OCIO config.")

        # Only colorconvert is implemented for now.

        # if self.ociolook:
        #     oiio.ImageBufAlgo.ociolook(buf, buf, self.ociolook, self.ocioview, colorconfig=self.ocioconfig)
        # if self.ociodisplay and self.ocioview:
        #     # Apply OCIO display transform onto specified image buffer
        #     success = oiio.ImageBufAlgo.ociodisplay(buf, buf, self.ociodisplay, self.ocioview, colorconfig=self.ocioconfig)

        return buf



    def generate_text(self, text_element_name, text_element, buf):
        """
        Generate text and write it into an image buffer.

        Args:
            text_element_name: the name of the text element to search for in the config
            text_element: the config dict to use
            buf: the oiio.ImageBuf object to write the pixels into

        Returns:
            Returns the modified oiio.ImageBuf object with text added.
        """

        # Text Elements
        log.debug("Processing text element: {0}".format(text_element_name))

        # Inherit globals if an element in text_element is not defined
        for key, value in text_element.items():
            if key in self.profile_config:
                if not text_element[key]:
                    # text element key is blank, inherit global value
                    text_element[key] = self.profile_config[key]
        font = text_element['font']
        if not os.path.isfile(font):
            log.error("Specified font does not exist!")
            return buf


        # Calculate font size and position
        font_size = text_element['font_size']
        font_color = text_element['font_color']
        box = text_element['box']
        justify = text_element['justify']
        if justify != "left" or justify != "center":
            justify = "left"


        # Scale back to pixels from %
        box_ll = [int(box[0] * self.output_width), int(box[1] * self.output_height)]
        box_ur = [int(box[2] * self.output_width), int(box[3] * self.output_height)]
        font_size = int(font_size * self.output_width)

        # Convert from Nuke-style (reference = lower left) to OIIO Style (reference = upper left)
        box_ll[1] = int(self.output_height - box_ll[1])
        box_ur[1] = int(self.output_height - box_ur[1])

        # Get text to display
        text_contents = self.text.get(text_element_name)
        text_prefix = text_element['prefix']
        if text_prefix:
            text_contents = text_prefix + text_contents

        leading = self.profile_config.get('leading')


        if text_contents:
            log.debug("Text Output: \n\t\t\t\t{0}, {1}, {2}, {fontsize}, {textcolor}, {shadow}".format(box_ll[0], box_ll[1], text_contents, fontsize=font_size, fontname=font,
                textcolor=(font_color[0], font_color[1], font_color[2], font_color[3]), shadow=0))

            text_roi = oiio.ImageBufAlgo.text_size(text_contents, fontsize=font_size, fontname=font)

            # Add text height to position
            box_ll[1] = int(box_ll[1] + text_roi.height)
            box_width = box_ur[0] - box_ll[0]

            # Wrap text into lines that are not longer than box width
            if text_roi.width > box_width:
                words = text_contents.split()
                # Gather a list of lines that fit in the box
                lines = []
                line = []
                for i, word in enumerate(words):
                    line.append(word)
                    # Get length of line with next word
                    str_line = " ".join(line)
                    if i < len(words)-1:
                        text_width = oiio.ImageBufAlgo.text_size(str_line + " " + words[i+1], fontsize=font_size, fontname=font).width
                    else:
                        text_width = oiio.ImageBufAlgo.text_size(str_line, fontsize=font_size, fontname=font).width
                    if text_width > box_width:
                        lines.append(str_line)
                        line = []
                    if i == len(words)-1:
                        lines.append(str_line)
            else:
                lines = [text_contents]

            lines.reverse()
            for line in lines:
                oiio.ImageBufAlgo.render_text(
                    buf, box_ll[0], box_ll[1], line, fontsize=font_size, fontname=font,
                    textcolor=(font_color[0], font_color[1], font_color[2], font_color[3]),
                    alignx=justify, aligny="bottom", shadow=0,
                    roi=oiio.ROI.All, nthreads=0
                    )
                # Offset up by line height + leading amount
                box_ll[1] = int(box_ll[1] - font_size - font_size * leading)
        else:
            log.warning("Warning: No text specified for text element {0}".format(text_element_name))
        return buf







if __name__=="__main__":
    daily = GenerateDaily()
    # if daily.setup_success:
    #     daily.process()