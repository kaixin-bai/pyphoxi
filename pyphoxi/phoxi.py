import logging
import json
import os
import socket
import struct
import time
import threading

import cv2
import numpy as np


class PhoXiSensor(object):
    """A PhoXi Sensor camera class.
    """
    def __init__(self, tcp_ip, tcp_port, resolution="low", calib_file=None):
        """Initializes the connection to the TCP server.
        """
        self._is_start = False

        self.resolution = resolution
        self.calib_file = calib_file

        self._tcp_ip = tcp_ip
        self._tcp_port = tcp_port
        self._buffer_size = 4096

        # initially, we set the packet size to 26 MB since
        # we just need to parse the first 8 bytes to figure
        # out the resolution of the image stream. Later,
        # adjust the value to the correct packet size.
        self._packet_size = 26 * 1024**2

        self._tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._init_params()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        try:
            self._tcp_socket.connect((self._tcp_ip, self._tcp_port))
            self._setup()
            time.sleep(1)
            self._is_start = True
        except:
            raise OSError("[!] Could not connect to the server. Make sure the C++ file is running.")

    def stop(self):
        if not self._is_start:
            logging.warning("[!] Sensor is not on.")
            return False
        self._tcp_socket.close()
        self._is_start = False
        return True

    def _setup(self):
        """Talks to the server and parses data parameters.
        """
        self._tcp_socket.settimeout(1)
        data = np.frombuffer(self._get_packet()[:8], np.int32)
        self._height, self._width = data
        self._num_pixels = self._height * self._width
        self._header_size = 3 * 4
        self._payload_size = self._num_pixels * 2 * 4
        self._packet_size = self._header_size + self._payload_size  # correct the packet size
        self._tcp_socket.settimeout(None)

    def _get_factory_intrinsics_distortion(self):
        """Reads factory intrinsics and distortion from the generated .txt file.
        """
        filename = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'intrinsic_parameters.txt')
        with open(filename) as f:
            lines = f.readlines()
        lines = [l.strip("\n") for l in lines]
        intr = np.array([
            [float(lines[2]), 0, float(lines[4])],
            [0, float(lines[2]), float(lines[7])],
            [0, 0, 1],
        ])
        if self.resolution == "low":
            intr /= 2
            intr[2, 2] = 1.
        dist = np.array([
            float(lines[15]),
            float(lines[16]), 
            float(lines[17]),
            float(lines[18]),
            float(lines[19]),
        ])
        return intr, dist

    def _set_default_params(self):
        """Sets default camera parameters.

        This is called if the calibration file cannot be read
        or isn't provided.
        """
        self._intr, self._dist = self._get_factory_intrinsics_distortion()
        self._extr = np.zeros((1, 6))

    def _init_params(self):
        """Initializes camera parameters.
        """
        if self.calib_file is not None:
            try:
                with open(self.calib_file, 'r') as fp:
                    params = json.load(fp)
                self._intr = np.array([
                    [params['intrinsics'][0], 0, params['intrinsics'][2]],
                    [0, params['intrinsics'][1], params['intrinsics'][3]],
                    [0, 0, 1]
                ])
                self._dist = np.array([params['distortion']])
                self._extr = np.array([params['extrinsics']])
            except:
                self._set_default_params()
        else:
            print("No calibration file provided. Using factory defaults.")
            self._set_default_params()

    def _get_packet(self):
        """Obtain a TCP packet from the server.
        """
        self._tcp_socket.send(b'blah')  # ping
        data = b''
        while len(data) < (self._packet_size):
            try:
                data += self._tcp_socket.recv(self._buffer_size)
            except socket.timeout:
                break
        data = data[:self._packet_size]
        return data

    def _process_packet(self, packet):
        """Extracts the data from the packet.
        """
        frame_id = np.frombuffer(packet[8:self._header_size], np.int32)[0]
        gray_img = np.frombuffer(packet[self._header_size:(self._header_size + self._num_pixels*4)], np.float32)
        depth_img = np.frombuffer(packet[(self._num_pixels*4 + self._header_size):], np.float32)

        # reshape
        gray_img = gray_img.copy().reshape(self._height, self._width)
        depth_img = depth_img.copy().reshape(self._height, self._width)

        # convert depth to meters
        depth_img = depth_img * 1e-3

        # convert 32-bit intensity to 8-bit grayscale
        gray_img = (gray_img - gray_img.min()) / (gray_img.max() - gray_img.min())
        gray_img = (gray_img * 255.).astype("uint8")

        return frame_id, gray_img, depth_img

    def get_frame(self, undistort=False):
        """Fetches the data from the TCP server.
        """
        frame_id, gray_img, depth_img = self._process_packet(self._get_packet())

        if undistort:
            H, W = gray_img.shape
            new_intr, _ = cv2.getOptimalNewCameraMatrix(self._intr, self._dist, (W, H), 1)
            map_x, map_y = cv2.initUndistortRectifyMap(self._intr, self._dist, None, new_intr, (W, H), cv2.CV_32FC1)
            gray_img = cv2.remap(gray_img, map_x, map_y, cv2.INTER_LINEAR)

        return frame_id, gray_img, depth_img

    @property
    def intrinsics(self):
        return self._intr

    @property
    def distortion(self):
        return self._dist

    @property
    def extrinsics(self):
        return self._extr