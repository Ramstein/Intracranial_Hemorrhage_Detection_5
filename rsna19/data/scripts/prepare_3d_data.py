""" Load dicom files using vtk package """
import shutil

import os
from glob import glob
from math import atan
from collections import namedtuple
import traceback
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

import numpy as np
import vtk
from scipy import ndimage
from vtk import vtkImageCast, vtkImageResample, vtkDICOMImageReader
from vtk.util.numpy_support import vtk_to_numpy
import tqdm

from rsna19.configs.base_config import BaseConfig

ShearParams = namedtuple('ShearParams', 'rad_tilt, minus_center_z')
OUT_SIZE = (400, 400)
BG_HU = -2000


class VtkImage:
    """Class for loading and scaling dicoms using Vtk library"""

    def __init__(self, scan_dir, spacing='auto'):
        """Load image to scale
        :param scan_dir: path to dicom file
        :param spacing: [x,y,z] spacing in mm, or 'auto' if we want to use min spacing already present in a scan,
               'none' if we are not doing any resamplig
        """

        # read dicom
        self.reader = vtkDICOMImageReader()
        self.reader.ReleaseDataFlagOff()
        self.reader.SetDirectoryName(scan_dir)
        self.reader.Update()

        # prepare parameters for shear transform (gantry tilt)
        x1, y1, z1, x2, y2, z2 = image_orientation = self.reader.GetImageOrientationPatient()

        # if non-standard orientation, then it's non-standard series
        if y2 == 0:
            raise Exception(f"Wrong patient orientation: {image_orientation}")

        rad_tilt = atan(z2 / y2)
        center_z = self.reader.GetOutput().GetBounds()[5] / 2
        self.shear_params = ShearParams(rad_tilt, -center_z)

        self.scan_dir = scan_dir
        self.spacing = spacing

        self.angle_z = 0
        self.angle_y = 0
        self.origin_x = None
        self.image = None

    def set_transform(self, angle_z, angle_y, origin_x):
        self.angle_z = angle_z
        self.angle_y = angle_y
        self.origin_x = origin_x
        self.image = None

    def set_spacing(self, spacing):
        self.spacing = spacing
        self.image = None

    def update_image(self):
        reslice = vtk.vtkImageReslice()

        if self.origin_x is not None:
            # add padding so that origin_x is in the middle of the image
            pad = vtk.vtkImageConstantPad()
            pad.SetInputConnection(self.reader.GetOutputPort())
            pad.SetConstant(BG_HU)

            # GetExtent() returns a tuple (minX, maxX, minY, maxY, minZ, maxZ)
            extent = list(self.reader.GetOutput().GetExtent())
            x_size = extent[1] - extent[0]
            extent[0] -= max(x_size - 2 * self.origin_x, 0)
            extent[1] += max(2 * self.origin_x - x_size, 0)
            pad.SetOutputWholeExtent(*extent)
            reslice.SetInputConnection(pad.GetOutputPort())
        else:
            reslice.SetInputConnection(self.reader.GetOutputPort())

        transform = vtk.vtkPerspectiveTransform()

        # gantry tilt
        transform.Shear(0, *self.shear_params)

        if self.angle_z != 0 or self.angle_y != 0:
            transform.RotateWXYZ(-self.angle_z, 0, 0, 1)  # top
            transform.RotateWXYZ(self.angle_y, 0, 1, 0)  # front

        reslice.SetResliceTransform(transform)
        reslice.SetInterpolationModeToCubic()
        reslice.AutoCropOutputOn()
        reslice.SetBackgroundLevel(BG_HU)
        reslice.Update()

        spacings_lists = reslice.GetOutput().GetSpacing()

        if self.spacing == 'auto':
            min_spacing = min(spacings_lists)
            if not min_spacing:
                raise ValueError('Invalid scan. Path: {}'.format(self.scan_dir))
            spacing = [min_spacing, min_spacing, min_spacing]

        elif self.spacing == 'none':
            spacing = None
        else:
            spacing = self.spacing

        if spacing is None:
            self.image = reslice
        else:
            resample = vtkImageResample()
            resample.SetInputConnection(reslice.GetOutputPort())
            resample.SetAxisOutputSpacing(0, spacing[0])  # x axis
            resample.SetAxisOutputSpacing(1, spacing[1])  # y axis
            resample.SetAxisOutputSpacing(2, spacing[2])  # z axis
            resample.SetInterpolationModeToCubic()
            resample.Update()

            self.image = resample

    def get_slices(self, dtype=np.float32):
        """Function that returns all slices in original size after gantry tilt handling"""

        if self.image is None:
            self.update_image()

        image = self.image.GetOutput()
        rows, cols, depth = image.GetDimensions()
        spacing = image.GetSpacing()

        scalars = image.GetPointData().GetScalars()
        array = vtk_to_numpy(scalars)

        array = array.reshape(depth, cols, rows)
        array = np.rot90(array, 2, axes=(0, 1))

        if dtype:
            array = array.astype(dtype)

        if len(array) < 5:
            raise Exception("Cannot read 3D dicom image")

        return array, spacing, self.shear_params


def crop_scan(scan, dest_shape):
    dest_shape = np.array(dest_shape)
    _, y, x = ndimage.measurements.center_of_mass(scan > 0)
    center = np.array([y, x], dtype=np.int32)
    corner0 = center - dest_shape // 2
    corner1 = corner0 + dest_shape

    corner0_clipped = np.maximum(corner0, 0)
    corner1_clipped = np.minimum(corner1, scan.shape[1:])
    margin0 = np.abs(corner0 - corner0_clipped)
    margin1 = np.abs(corner1 - corner1_clipped)

    scan_cropped = np.zeros((scan.shape[0], dest_shape[0], dest_shape[1]), dtype=np.int16) + BG_HU
    crop = scan[:, corner0_clipped[0]:corner1_clipped[0], corner0_clipped[1]:corner1_clipped[1]]
    scan_cropped[:, margin0[0]:dest_shape[0] - margin1[0], margin0[1]:dest_shape[1] - margin1[1]] = crop

    return scan_cropped


def process_scan(scan_dir):
    try:
        out_dir = scan_dir.replace('dicom/', '3d/')
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)

        out_npy = VtkImage(scan_dir, spacing='none').get_slices()[0]
        out_npy = crop_scan(out_npy, OUT_SIZE)

        for idx, scan_slice in enumerate(out_npy):
            np.save(f'{out_dir}{idx:03d}.npy', scan_slice.astype(np.int16))

    except Exception:
        traceback.print_exc()
        print(scan_dir)


def main():
    with ProcessPoolExecutor(max_workers=16) as executor:
        paths = glob(f'{BaseConfig.data_root}/train/*/dicom/') + glob(f'{BaseConfig.data_root}/test/*/dicom/')
        list(tqdm.tqdm(executor.map(process_scan, paths), total=len(paths)))


if __name__ == '__main__':
    main()
