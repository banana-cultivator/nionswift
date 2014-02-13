# standard libraries
import copy
import gettext
import logging
import math
import sys
import threading
import uuid
import weakref

# third party libraries
import numpy
import scipy
import scipy.fftpack
import scipy.ndimage

# local libraries
from nion.swift import Decorators
from nion.swift.Decorators import timeit
from nion.swift import Image
from nion.swift import DataItem
from nion.swift import Graphics
from nion.swift import Storage
from nion.ui import UserInterfaceUtility

_ = gettext.gettext


class OperationItem(Storage.StorageBase):
    """
        OperationItem represents an operation on numpy data array.
        Pass in a description during construction. The description
        should describe what parameters are editable and how they
        are connected to the operation.
    """
    def __init__(self, operation_id):
        Storage.StorageBase.__init__(self)

        self.storage_type = "operation"

        # an operation gets one chance to find its behavior. if the behavior doesn't exist
        # then it will simply provide null data according to the saved parameters. if there
        # are no saved parameters, defaults are used.
        self.operation = OperationManager().build_operation(operation_id)

        self.name = self.operation.name if self.operation else _("Unavailable Operation")
        self.__enabled = True

        # operation_id is immutable
        self.operation_id = operation_id

        # manage properties
        self.description = self.operation.description if self.operation else []
        self.properties = [description_entry["property"] for description_entry in self.description]
        self.values = {}

        # manage graphics
        self.graphic = None

        self.storage_properties += ["operation_id", "enabled", "values"]  # "dtype", "shape"
        self.storage_items += ["graphic"]

    # called when remove_ref causes ref_count to go to 0
    def about_to_delete(self):
        self.set_graphic("graphic", None)
        super(OperationItem, self).about_to_delete()

    @classmethod
    def build(cls, datastore, item_node, uuid_):
        operation_id = datastore.get_property(item_node, "operation_id")
        operation = cls(operation_id)
        operation.enabled = datastore.get_property(item_node, "enabled", True)
        operation.values = datastore.get_property(item_node, "values", dict())
        graphic = datastore.get_item(item_node, "graphic")
        operation.set_graphic("graphic", graphic)
        return operation

    def create_editor(self, ui):
        return None

    # enabled property
    def __get_enabled(self):
        return self.__enabled
    def __set_enabled(self, enabled):
        self.__enabled = enabled
        self.notify_set_property("enabled", enabled)
    enabled = property(__get_enabled, __set_enabled)

    # get a property.
    def get_property(self, property_id, default_value=None):
        if property_id in self.values:
            return self.values[property_id]
        if default_value is not None:
            return default_value
        for description_entry in self.description:
            if description_entry["property"] == property_id:
                return description_entry.get("default")
        return None

    # set a property.
    def set_property(self, property_id, value):
        self.values[property_id] = value
        if self.operation:
            setattr(self.operation, property_id, value)
        self.notify_set_property("values", self.values)

    # update the default value for this operation.
    def __set_property_default(self, property_id, default_value):
        for description_entry in self.description:
            if description_entry["property"] == property_id:
                description_entry["default"] = default_value
                if property_id not in self.values or self.values[property_id] is None:
                    self.values[property_id] = default_value
                    if self.operation:
                        setattr(self.operation, property_id, default_value)

    # clients call this to perform processing
    def process_data(self, data):
        if self.operation:
            return self.operation.process_data_in_place(data)
        else:
            return data.copy()

    # calibrations

    def get_processed_calibrations(self, data_shape, data_dtype, source_calibrations):
        if self.operation:
            return self.operation.get_processed_calibrations(data_shape, data_dtype, source_calibrations)
        else:
            return source_calibrations

    def get_processed_intensity_calibration(self, data_shape, data_dtype, intensity_calibration):
        if self.operation:
            return self.operation.get_processed_intensity_calibration(data_shape, data_dtype, intensity_calibration)
        else:
            return source_calibrations

    # data shape and type
    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        if self.operation:
            return self.operation.get_processed_data_shape_and_dtype(data_shape, data_dtype)
        return data_shape, data_dtype

    # default value handling.
    def update_data_shape_and_dtype(self, data_shape, data_dtype):
        if self.operation:
            default_values = self.operation.property_defaults_for_data_shape_and_dtype(data_shape, data_dtype)
            for property, default_value in default_values.iteritems():
                self.__set_property_default(property, default_value)

    # subclasses should override __deepcopy__ and deepcopy_from as necessary
    def __deepcopy__(self, memo):
        operation = self.__class__(self.operation_id)
        operation.deepcopy_from(self, memo)
        memo[id(self)] = operation
        return operation

    def deepcopy_from(self, operation, memo):
        values = copy.deepcopy(operation.values)
        # copy one by one to keep default values for missing keys
        for key in values.keys():
            self.values[key] = values[key]
        # TODO: Check use of memo here.
        if operation.graphic:
            self.set_graphic("graphic", operation.graphic)
        else:
            self.set_graphic("graphic", None)
        self.__enabled = operation.enabled

    def notify_set_property(self, key, value):
        super(OperationItem, self).notify_set_property(key, value)
        self.notify_listeners("operation_changed", self)

    def get_storage_item(self, key):
        if key == "graphic":
            return self.graphic
        return super(OperationItem, self).get_storage_item(key)

    def get_graphic(self, key):
        return self.get_storage_item(key)

    def set_graphic(self, key, graphic):
        if key == "graphic":
            if self.graphic:
                self.notify_clear_item("graphic")
                self.graphic.remove_observer(self)
                self.graphic.remove_ref()
                self.graphic = None
            if graphic:
                self.graphic = graphic
                graphic.add_observer(self)
                graphic.add_ref()
                self.notify_set_item("graphic", graphic)
                self.__sync_graphic()

    def __sync_graphic(self):
        for description_entry in self.description:
            type = description_entry["type"]
            property_id = description_entry["property"]
            if type == "line" and isinstance(self.graphic, Graphics.LineGraphic):
                value = self.graphic.start, self.graphic.end
                self.values[property_id] = value
                if self.operation:
                    setattr(self.operation, property_id, value)
            elif type == "rectangle" and isinstance(self.graphic, Graphics.RectangleGraphic):
                value = self.graphic.bounds
                self.values[property_id] = value
                if self.operation:
                    setattr(self.operation, property_id, value)

    # watch for changes to graphic item and try to associate with the description. hacky.
    def property_changed(self, object, key, value):
        if object is not None and object == self.graphic:
            self.__sync_graphic()
            self.notify_listeners("operation_changed", self)


class Operation(object):

    def __init__(self, name, operation_id, description=None):
        self.name = name
        self.operation_id = operation_id
        self.description = description if description else []

    # handle properties from the description of the operation.
    def get_property(self, property_id, default_value=None):
        return getattr(self, property_id) if hasattr(self, property_id) else default_value

    # subclasses can override this method to perform processing on a copy of the original data
    # this method should return either the copy itself or a new data set
    def process_data_copy(self, data_copy):
        raise NotImplementedError

    # subclasses can override this method to perform processing on the original data.
    # this method should always return a new copy of data
    def process_data_in_place(self, data):
        return self.process_data_copy(data.copy())

    # calibrations
    def get_processed_calibrations(self, data_shape, data_dtype, source_calibrations):
        return source_calibrations
    def get_processed_intensity_calibration(self, data_shape, data_dtype, intensity_calibration):
        return intensity_calibration

    # subclasses that change the type or shape of the data must override
    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        return data_shape, data_dtype

    # default value handling. this gives the operation a chance to update default
    # values when the data shape or dtype changes.
    def property_defaults_for_data_shape_and_dtype(self, data_shape, data_dtype):
        return dict()


class FFTOperation(Operation):

    def __init__(self):
        super(FFTOperation, self).__init__(_("FFT"), "fft-operation")

    def process_data_in_place(self, data):
        if Image.is_data_1d(data):
            return scipy.fftpack.fftshift(scipy.fftpack.fft(data))
        elif Image.is_data_2d(data):
            data_copy = data.copy()  # let other threads use data while we're processing
            return scipy.fftpack.fftshift(scipy.fftpack.fft2(data_copy))
        else:
            raise NotImplementedError()

    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        return data_shape, numpy.dtype(numpy.complex128)

    def get_processed_calibrations(self, data_shape, data_dtype, source_calibrations):
        assert len(source_calibrations) == len(Image.spatial_shape_from_shape_and_dtype(data_shape, data_dtype))
        return [DataItem.Calibration(0.0,
                                     1.0 / (source_calibrations[i].scale * data_shape[i]),
                                     "1/" + source_calibrations[i].units) for i in range(len(source_calibrations))]


class IFFTOperation(Operation):

    def __init__(self):
        super(IFFTOperation, self).__init__(_("Inverse FFT"), "inverse-fft-operation")

    def process_data_in_place(self, data):
        if Image.is_data_1d(data):
            return scipy.fftpack.fftshift(scipy.fftpack.ifft(data))
        elif Image.is_data_2d(data):
            return scipy.fftpack.ifft2(scipy.fftpack.ifftshift(data))
        else:
            raise NotImplementedError()

    def get_processed_calibrations(self, data_shape, data_dtype, source_calibrations):
        assert len(source_calibrations) == len(Image.spatial_shape_from_shape_and_dtype(data_shape, data_dtype))
        return [DataItem.Calibration(0.0,
                                     1.0 / (source_calibrations[i].scale * data_shape[i]),
                                     "1/" + source_calibrations[i].units) for i in range(len(source_calibrations))]


class InvertOperation(Operation):

    def __init__(self):
        super(InvertOperation, self).__init__(_("Invert"), "invert-operation")

    def process_data_in_place(self, data_copy):
        if Image.is_data_rgba(data_copy) or Image.is_data_rgb(data_copy):
            if Image.is_data_rgba(data_copy):
                inverted = 255 - data_copy[:]
                inverted[...,3] = data_copy[...,3]
                return inverted
            else:
                return 255 - data_copy[:]
        else:
            return 1.0 - data_copy[:]


class GaussianBlurOperation(Operation):

    def __init__(self):
        description = [
            { "name": _("Radius"), "property": "sigma", "type": "scalar", "default": 0.3 }
        ]
        super(GaussianBlurOperation, self).__init__(_("Gaussian Blur"), "gaussian-blur-operation", description)
        self.sigma = 0.3

    def process_data_in_place(self, data_copy):
        return scipy.ndimage.gaussian_filter(data_copy, sigma=10*self.get_property("sigma"))


class Crop2dOperation(Operation):

    def __init__(self):
        description = [
            { "name": _("Bounds"), "property": "bounds", "type": "rectangle", "default": ((0.0, 0.0), (1.0, 1.0)) }
        ]
        super(Crop2dOperation, self).__init__(_("Crop"), "crop-operation", description)
        self.bounds = (0.0, 0.0), (1.0, 1.0)

    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        shape = data_shape
        bounds = self.get_property("bounds")
        bounds_int = ((int(shape[0] * bounds[0][0]), int(shape[1] * bounds[0][1])), (int(shape[0] * bounds[1][0]), int(shape[1] * bounds[1][1])))
        if Image.is_shape_and_dtype_rgba(data_shape, data_dtype) or Image.is_shape_and_dtype_rgb(data_shape, data_dtype):
            return bounds_int[1] + (data_shape[-1], ), data_dtype
        else:
            return bounds_int[1], data_dtype

    def process_data_in_place(self, data):
        shape = data.shape
        bounds = self.get_property("bounds")
        bounds_int = ((int(shape[0] * bounds[0][0]), int(shape[1] * bounds[0][1])), (int(shape[0] * bounds[1][0]), int(shape[1] * bounds[1][1])))
        return data[bounds_int[0][0]:bounds_int[0][0] + bounds_int[1][0], bounds_int[0][1]:bounds_int[0][1] + bounds_int[1][1]].copy()


class Resample2dOperation(Operation):

    def __init__(self):
        description = [
            {"name": _("Width"), "property": "width", "type": "integer-field", "default": None},
            {"name": _("Height"), "property": "height", "type": "integer-field", "default": None},
        ]
        super(Resample2dOperation, self).__init__(_("Resample"), "resample-operation", description)
        self.width = 0
        self.height = 0

    def process_data_copy(self, data_copy):
        height = self.get_property("height", data_copy.shape[0])
        width = self.get_property("width", data_copy.shape[1])
        if data_copy.shape[1] == width and data_copy.shape[0] == height:
            return data_copy
        return Image.scaled(data_copy, (height, width))

    def get_processed_calibrations(self, data_shape, data_dtype, source_calibrations):
        assert len(source_calibrations) == 2
        height = self.get_property("height", data_shape[0])
        width = self.get_property("width", data_shape[1])
        dimensions = (height, width)
        return [DataItem.Calibration(source_calibrations[i].origin,
                                     source_calibrations[i].scale * data_shape[i] / dimensions[i],
                                     source_calibrations[i].units) for i in range(len(source_calibrations))]

    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        height = self.get_property("height", data_shape[0])
        width = self.get_property("width", data_shape[1])
        if Image.is_shape_and_dtype_rgba(data_shape, data_dtype) or Image.is_shape_and_dtype_rgb(data_shape, data_dtype):
            return (height, width, data_shape[-1]), data_dtype
        else:
            return (height, width), data_dtype

    def property_defaults_for_data_shape_and_dtype(self, data_shape, data_dtype):
        property_defaults = {
            "height": data_shape[0],
            "width": data_shape[1],
        }
        return property_defaults


class HistogramOperation(Operation):

    def __init__(self):
        super(HistogramOperation, self).__init__(_("Histogram"), "histogram-operation")
        self.bins = 256

    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        return (self.bins, ), numpy.dtype(numpy.int)

    def process_data_in_place(self, data):
        histogram_data = numpy.histogram(data, bins=self.bins)
        return histogram_data[0].astype(numpy.int)


class LineProfileOperation(Operation):

    def __init__(self):
        description = [
            { "name": _("Vector"), "property": "vector", "type": "line", "default": ((0.25, 0.25), (0.75, 0.75)) }
        ]
        super(LineProfileOperation, self).__init__(_("Line Profile"), "line-profile-operation", description)
        self.vector = (0.25, 0.25), (0.75, 0.75)

    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        start, end = self.get_property("vector")
        shape = data_shape
        start_data = (int(shape[0]*start[0]), int(shape[1]*start[1]))
        end_data = (int(shape[0]*end[0]), int(shape[1]*end[1]))
        length = int(math.sqrt((end_data[1] - start_data[1])**2 + (end_data[0] - start_data[0])**2))
        if Image.is_shape_and_dtype_rgba(data_shape, data_dtype) or Image.is_shape_and_dtype_rgb(data_shape, data_dtype):
            return (length, data_shape[-1]), data_dtype
        else:
            return (length, ), numpy.dtype(numpy.double)

    def get_processed_calibrations(self, data_shape, data_dtype, source_calibrations):
        return [DataItem.Calibration(0.0, source_calibrations[0].scale, source_calibrations[0].units)]

    def process_data_in_place(self, data):
        start, end = self.get_property("vector")
        shape = data.shape
        start_data = (int(shape[0]*start[0]), int(shape[1]*start[1]))
        end_data = (int(shape[0]*end[0]), int(shape[1]*end[1]))
        length = int(math.sqrt((end_data[1] - start_data[1])**2 + (end_data[0] - start_data[0])**2))
        if length > 0:
            c0 = numpy.linspace(start_data[0], end_data[0]-1, length)
            c1 = numpy.linspace(start_data[1], end_data[1]-1, length)
            return data[c0.astype(numpy.int), c1.astype(numpy.int)]
        return numpy.zeros((1))


class ConvertToScalarOperation(Operation):

    def __init__(self):
        super(ConvertToScalarOperation, self).__init__(_("Convert to Scalar"), "convert-to-scalar-operation")

    def process_data_in_place(self, data):
        if Image.is_data_rgba(data) or Image.is_data_rgb(data):
            return Image.convert_to_grayscale(data, numpy.double)
        else:
            return data.copy()

    def get_processed_data_shape_and_dtype(self, data_shape, data_dtype):
        if Image.is_shape_and_dtype_rgba(data_shape, data_dtype) or Image.is_shape_and_dtype_rgb(data_shape, data_dtype):
            return data_shape[:-1], numpy.dtype(numpy.double)
        return data_shape, data_dtype


class OperationPropertyBinding(UserInterfaceUtility.Binding):

    """
        Binds to a property of an operation object.

        This object records the 'values' property of the operation. Then it
        watches for changes to 'values' which match the watched property.
    """

    def __init__(self, source, property_name, converter=None):
        super(OperationPropertyBinding, self).__init__(source,  converter)
        self.__property_name = property_name
        self.source_setter = lambda value: self.source.set_property(self.__property_name, value)
        self.source_getter = lambda: self.source.get_property(self.__property_name)
        # use this to know when a specific property changes
        self.__values = copy.copy(source.values)

    # thread safe
    def property_changed(self, sender, property, property_value):
        if sender == self.source and property == "values":
            values = property_value
            new_value = values.get(self.__property_name)
            old_value = self.__values.get(self.__property_name)
            if new_value != old_value:
                # perform on the main thread
                self.add_task("update_target", lambda: self.update_target(new_value))
                self.__values = copy.copy(self.source.values)


class OperationManager(object):
    __metaclass__ = Decorators.Singleton

    def __init__(self):
        self.__operations = dict()

    def register_operation(self, operation_id, create_operation_fn):
        self.__operations[operation_id] = create_operation_fn

    def unregister_operation(self, operation_id):
        del self.__operations[operation_id]

    def build_operation(self, operation_id):
        if operation_id in self.__operations:
            return self.__operations[operation_id]()
        return None


OperationManager().register_operation("fft-operation", lambda: FFTOperation())
OperationManager().register_operation("inverse-fft-operation", lambda: IFFTOperation())
OperationManager().register_operation("invert-operation", lambda: InvertOperation())
OperationManager().register_operation("gaussian-blur-operation", lambda: GaussianBlurOperation())
OperationManager().register_operation("crop-operation", lambda: Crop2dOperation())
OperationManager().register_operation("resample-operation", lambda: Resample2dOperation())
OperationManager().register_operation("histogram-operation", lambda: HistogramOperation())
OperationManager().register_operation("line-profile-operation", lambda: LineProfileOperation())
OperationManager().register_operation("convert-to-scalar-operation", lambda: ConvertToScalarOperation())
