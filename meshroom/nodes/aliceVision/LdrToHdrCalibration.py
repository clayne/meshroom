__version__ = "3.1"

import json
import math
import os
from collections import Counter

from meshroom.core import desc
from meshroom.core.utils import COLORSPACES, VERBOSE_LEVEL

def findMetadata(d, keys, defaultValue):
    v = None
    for key in keys:
        v = d.get(key, None)
        k = key.lower()
        if v is not None:
            return v
        for dk, dv in d.items():
            dkm = dk.lower().replace(" ", "")
            if dkm == key.lower():
                return dv
            dkm = dkm.split(":")[-1]
            dkm = dkm.split("/")[-1]
            if dkm == k:
                return dv
    return defaultValue



class LdrToHdrCalibration(desc.AVCommandLineNode):
    commandLine = 'aliceVision_LdrToHdrCalibration {allParams}'
    size = desc.DynamicNodeSize('input')
    cpu = desc.Level.INTENSIVE
    ram = desc.Level.NORMAL

    category = 'Panorama HDR'
    documentation = '''
Calibrate LDR to HDR response curve from samples.
'''

    inputs = [
        desc.File(
            name="input",
            label="SfMData",
            description="Input SfMData file.",
            value="",
        ),
        desc.File(
            name="samples",
            label="Samples Folder",
            description="Samples folder.",
            value=desc.Node.internalFolder,
        ),
        desc.IntParam(
            name="userNbBrackets",
            label="Number Of Brackets",
            description="Number of exposure brackets per HDR image (0 for automatic detection).",
            value=0,
            range=(0, 15, 1),
            invalidate=False,
            group="user",  # not used directly on the command line
            errorMessage="The set number of brackets is not a multiple of the number of input images.\n"
                         "Errors will occur during the computation.",
            exposed=True,
        ),
        desc.IntParam(
            name="nbBrackets",
            label="Automatic Nb Brackets",
            description="Number of exposure brackets used per HDR image.\n"
                        "It is detected automatically from input Viewpoints metadata if 'userNbBrackets' is 0,\n"
                        "else it is equal to 'userNbBrackets'.",
            value=0,
            range=(0, 15, 1),
            group="bracketsParams",
        ),
        desc.BoolParam(
            name="byPass",
            label="Bypass",
            description="Bypass HDR creation and use the medium bracket as the source for the next steps.",
            value=False,
            enabled=lambda node: node.nbBrackets.value != 1,
            exposed=True,
        ),
        desc.ChoiceParam(
            name="calibrationMethod",
            label="Calibration Method",
            description="Method used for camera calibration:\n"
                        " - Auto: If RAW images are detected, the 'Linear' calibration method will be used. Otherwise, the 'Debevec'  calibration method will be used.\n"
                        " - Linear: Disables the calibration and assumes a linear Camera Response Function. If images are encoded in a known colorspace (like sRGB for JPEG), they will be automatically converted to linear.\n"
                        " - Debevec: Standard method for HDR calibration.\n"
                        " - Grossberg: Based on a learned database of cameras, allows to reduce the Camera Response Function to a few parameters while keeping all the precision.\n"
                        " - Laguerre: Simple but robust method estimating the minimal number of parameters.",
            values=["auto", "linear", "debevec", "grossberg", "laguerre"],
            value="auto",
            exclusive=True,
            enabled=lambda node: node.byPass.enabled and not node.byPass.value,
            exposed=True,
        ),
        desc.ChoiceParam(
            name="calibrationWeight",
            label="Calibration Weight",
            description="Weight function used to calibrate camera response:\n"
                        " - default (automatically selected according to the calibrationMethod)\n"
                        " - gaussian\n"
                        " - triangle\n"
                        " - plateau",
            value="default",
            values=["default", "gaussian", "triangle", "plateau"],
            exclusive=True,
            enabled=lambda node: node.byPass.enabled and not node.byPass.value,
        ),
        desc.IntParam(
            name="channelQuantizationPower",
            label="Channel Quantization Power",
            description="Quantization level like 8 bits or 10 bits.",
            value=10,
            range=(8, 14, 1),
            advanced=True,
            enabled=lambda node: node.byPass.enabled and not node.byPass.value,
            exposed=True,
        ),
        desc.ChoiceParam(
            name="workingColorSpace",
            label="Working Color Space",
            description="Color space in which the data are processed.\n"
                        "If 'auto' is selected, the working color space will be 'Linear' if RAW images are detected; otherwise, it will be set to 'sRGB'.",
            values=COLORSPACES,
            value="AUTO",
            exclusive=True,
            invalidate=False,
            group="user",  # not used directly on the command line
            enabled=lambda node: node.byPass.enabled and not node.byPass.value,
            exposed=True,
        ),
        desc.IntParam(
            name="maxTotalPoints",
            label="Max Number Of Points",
            description="Maximum number of points used from the sampling.\n"
                        "This ensures that the number of pixels values extracted by the sampling\n"
                        "can be managed by the calibration step (in term of computation time and memory usage).",
            value=1000000,
            range=(8, 10000000, 1000),
            advanced=True,
            enabled=lambda node: node.byPass.enabled and not node.byPass.value,
        ),
        desc.ChoiceParam(
            name="verboseLevel",
            label="Verbose Level",
            description="Verbosity level (fatal, error, warning, info, debug, trace).",
            values=VERBOSE_LEVEL,
            value="info",
            exclusive=True,
        ),
    ]

    outputs = [
       desc.File(
            name="response",
            label="Response File",
            description="Path to the output response file.",
            value=desc.Node.internalFolder + "response_<INTRINSIC_ID>.csv",
        ),
    ]

    def processChunk(self, chunk):
        if chunk.node.nbBrackets.value == 1:
            return
        # Trick to avoid sending --nbBrackets to the command line when the bracket detection is automatic.
        # Otherwise, the AliceVision executable has no way of determining whether the bracket detection was automatic
        # or if it was hard-set by the user.
        self.commandLine = "aliceVision_LdrToHdrCalibration {allParams}"
        if chunk.node.userNbBrackets.value == chunk.node.nbBrackets.value:
            self.commandLine += "{bracketsParams}"
        super(LdrToHdrCalibration, self).processChunk(chunk)

    @classmethod
    def update(cls, node):
        if not isinstance(node.nodeDesc, cls):
            raise ValueError("Node {} is not an instance of type {}".format(node, cls))
        # TODO: use Node version for this test
        if "userNbBrackets" not in node.getAttributes().keys():
            # Old version of the node
            return
        node.userNbBrackets.validValue = True  # Reset the status of "userNbBrackets"

        cameraInitOutput = node.input.getLinkParam(recursive=True)
        if not cameraInitOutput:
            node.nbBrackets.value = 0
            return
        if node.userNbBrackets.value != 0:
            # The number of brackets has been manually forced: check whether it is valid or not
            if cameraInitOutput and cameraInitOutput.node and cameraInitOutput.node.hasAttribute("viewpoints"):
                viewpoints = cameraInitOutput.node.viewpoints.value
                # The number of brackets should be a multiple of the number of input images
                if (len(viewpoints) % node.userNbBrackets.value != 0):
                    node.userNbBrackets.validValue = False
                else:
                    node.userNbBrackets.validValue = True
            node.nbBrackets.value = node.userNbBrackets.value
            return

        if not cameraInitOutput.node.hasAttribute("viewpoints"):
            if cameraInitOutput.node.hasAttribute("input"):
                cameraInitOutput = cameraInitOutput.node.input.getLinkParam(recursive=True)
        if cameraInitOutput and cameraInitOutput.node and cameraInitOutput.node.hasAttribute("viewpoints"):
            viewpoints = cameraInitOutput.node.viewpoints.value
        else:
            # No connected CameraInit
            node.nbBrackets.value = 0
            return

        inputs = []
        for viewpoint in viewpoints:
            jsonMetadata = viewpoint.metadata.value
            if not jsonMetadata:
                # no metadata, we cannot find the number of brackets
                node.nbBrackets.value = 0
                return
            d = json.loads(jsonMetadata)

            # Find Fnumber
            fnumber = findMetadata(d, ["FNumber"], "")
            if fnumber == "":
                aperture = findMetadata(d, ["Exif:ApertureValue", "ApertureValue", "Aperture"], "")
                if aperture == "":
                    fnumber = -1.0
                else:
                    fnumber = pow(2.0, aperture / 2.0)

            # Get shutter speed and ISO
            shutterSpeed = findMetadata(d, ["ExposureTime", "Exif:ShutterSpeedValue", "ShutterSpeedValue", "ShutterSpeed"], -1.0)
            iso = findMetadata(d, ["Exif:PhotographicSensitivity", "PhotographicSensitivity", "Photographic Sensitivity", "ISO"], -1.0)

            if not fnumber and not shutterSpeed:
                # If one image without shutter or fnumber, we cannot found the number of brackets.
                # We assume that there is no multi-bracketing, so nothing to do.
                node.nbBrackets.value = 1
                return
            inputs.append((viewpoint.path.value, (float(fnumber), float(shutterSpeed), float(iso))))
        inputs.sort()

        exposureGroups = []
        exposures = []
        prevFnumber = 0.0
        prevShutterSpeed = 0.0
        prevIso = 0.0
        prevPath = None  # Stores the dirname of the previous parsed image
        prevExposure = None
        newGroup = False  # True if a new exposure group needs to be created (useful when there are several datasets)
        for path, exp in inputs:
            # If the dirname of the previous image and the dirname of the current image do not match, this means that the
            # dataset that is being parsed has changed. A new group needs to be created but will fail to be detected in the
            # next "if" statement if the new dataset's exposure levels are different. Setting "newGroup" to True prevents this
            # from happening.
            if prevPath is not None and prevPath != os.path.dirname(path):
                newGroup = True

            currentExposure = LdrToHdrCalibration.getExposure(exp)

            # Create a new group if the current image's exposure level is smaller than the previous image's, or
            # if a new dataset has been detected (with a change in the path of the images).
            if prevExposure and currentExposure < prevExposure or newGroup:
                exposureGroups.append(exposures)
                exposures = [exp]
            else:
                exposures.append(exp)

            prevPath = os.path.dirname(path)
            prevExposure = currentExposure
            newGroup = False

        exposureGroups.append(exposures)

        exposures = None
        bracketSizes = Counter()
        if len(exposureGroups) == 1:
            if len(set(exposureGroups[0])) == 1:
                # Single exposure and multiple views
                node.nbBrackets.value = 1
            else:
                # Single view and multiple exposures
                node.nbBrackets.value = len(exposureGroups[0])
        else:
            for expGroup in exposureGroups:
                bracketSizes[len(expGroup)] += 1

            if len(bracketSizes) == 0:
                node.nbBrackets.value = 0
            else:
                bestTuple = None
                for tuple in bracketSizes.most_common():
                    if bestTuple is None or tuple[1] > bestTuple[1]:
                        bestTuple = tuple
                    elif tuple[1] == bestTuple[1]:
                        bestTuple = tuple if tuple[0] > bestTuple[0] else bestTuple

                bestBracketSize = bestTuple[0]
                node.nbBrackets.value = bestBracketSize

    @staticmethod
    def getExposure(exp, refIso = 100.0, refFnumber = 1.0):
        fnumber, shutterSpeed, iso = exp

        validShutterSpeed = shutterSpeed > 0.0 and math.isfinite(shutterSpeed)
        validFnumber = fnumber > 0.0 and math.isfinite(fnumber)

        if not validShutterSpeed and not validFnumber:
            return -1.0

        validRefFnumber = refFnumber > 0.0 and math.isfinite(refFnumber)

        if not validShutterSpeed:
            shutterSpeed = 1.0 / 200.0

        if not validFnumber:
            if validRefFnumber:
                fnumber = refFnumber
            else:
                fnumber = 2.0

        lRefFnumber = refFnumber
        if not validRefFnumber:
            lRefFnumber = fnumber

        isoToAperture = 1.0
        if iso > 1e-6 and refIso > 1e-6:
            isoToAperture = math.sqrt(iso / refIso)

        newFnumber = fnumber * isoToAperture
        expIncrease = (lRefFnumber / newFnumber) * (lRefFnumber / newFnumber)

        return shutterSpeed * expIncrease
