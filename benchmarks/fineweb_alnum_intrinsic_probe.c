#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *
alnum_ratio_ok(PyObject *self, PyObject *args, PyObject *kwargs)
{
    static char *keywords[] = {"text", "min_ratio", NULL};
    PyObject *text;
    double min_ratio;

    (void)self;
    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "O!d:alnum_ratio_ok",
            keywords,
            &PyUnicode_Type,
            &text,
            &min_ratio)) {
        return NULL;
    }

    const Py_ssize_t length = PyUnicode_GET_LENGTH(text);
    if (length == 0) {
        Py_RETURN_FALSE;
    }

    Py_ssize_t alnum = 0;
    const int kind = PyUnicode_KIND(text);
    const void *data = PyUnicode_DATA(text);
    if (kind == PyUnicode_1BYTE_KIND && PyUnicode_IS_ASCII(text)) {
        const Py_UCS1 *ascii = (const Py_UCS1 *)data;
        for (Py_ssize_t index = 0; index < length; index++) {
            const Py_UCS1 value = ascii[index];
            alnum += ((value >= '0' && value <= '9') ||
                      (value >= 'A' && value <= 'Z') ||
                      (value >= 'a' && value <= 'z'));
        }
    }
    else {
        for (Py_ssize_t index = 0; index < length; index++) {
            alnum += Py_UNICODE_ISALNUM(
                PyUnicode_READ(kind, data, index));
        }
    }

    if (((double)alnum / (double)length) >= min_ratio) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

static PyObject *
punctuation_normalize(PyObject *self, PyObject *text)
{
    (void)self;
    if (!PyUnicode_Check(text)) {
        PyErr_SetString(PyExc_TypeError, "text must be str");
        return NULL;
    }

    const Py_ssize_t length = PyUnicode_GET_LENGTH(text);
    const int kind = PyUnicode_KIND(text);
    const void *data = PyUnicode_DATA(text);
    void *output = PyMem_Malloc((size_t)length * (size_t)kind);
    if (output == NULL && length != 0) {
        return PyErr_NoMemory();
    }

    for (Py_ssize_t index = 0; index < length; index++) {
        Py_UCS4 value = PyUnicode_READ(kind, data, index);
        switch (value) {
            case 0x201c:
            case 0x201d:
                value = '"';
                break;
            case 0x2018:
            case 0x2019:
                value = '\'';
                break;
            case 0x2013:
            case 0x2014:
                value = '-';
                break;
            default:
                break;
        }
        PyUnicode_WRITE(kind, output, index, value);
    }

    PyObject *result = PyUnicode_FromKindAndData(kind, output, length);
    PyMem_Free(output);
    return result;
}

static PyObject *
whitespace_normalize(PyObject *self, PyObject *text)
{
    (void)self;
    if (!PyUnicode_Check(text)) {
        PyErr_SetString(PyExc_TypeError, "text must be str");
        return NULL;
    }

    const Py_ssize_t length = PyUnicode_GET_LENGTH(text);
    const int kind = PyUnicode_KIND(text);
    const void *data = PyUnicode_DATA(text);
    void *output = PyMem_Malloc((size_t)length * (size_t)kind);
    if (output == NULL && length != 0) {
        return PyErr_NoMemory();
    }

    Py_ssize_t output_length = 0;
    int pending_space = 0;
    for (Py_ssize_t index = 0; index < length; index++) {
        const Py_UCS4 value = PyUnicode_READ(kind, data, index);
        if (Py_UNICODE_ISSPACE(value)) {
            pending_space = output_length != 0;
            continue;
        }
        if (pending_space) {
            PyUnicode_WRITE(kind, output, output_length++, ' ');
            pending_space = 0;
        }
        PyUnicode_WRITE(kind, output, output_length++, value);
    }

    PyObject *result = PyUnicode_FromKindAndData(
        kind,
        output,
        output_length);
    PyMem_Free(output);
    return result;
}

static PyMethodDef probe_methods[] = {
    {
        "alnum_ratio_ok",
        (PyCFunction)(void(*)(void))alnum_ratio_ok,
        METH_VARARGS | METH_KEYWORDS,
        "Return whether the Unicode alphanumeric ratio meets the threshold.",
    },
    {
        "punctuation_normalize",
        punctuation_normalize,
        METH_O,
        "Normalize the fixed punctuation mapping in one Unicode scan.",
    },
    {
        "whitespace_normalize",
        whitespace_normalize,
        METH_O,
        "Collapse Unicode whitespace and trim the ends in one scan.",
    },
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef probe_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "_fineweb_alnum_probe",
    .m_doc = "Diagnostic prototype for a CinderX Unicode scan intrinsic.",
    .m_size = -1,
    .m_methods = probe_methods,
};

PyMODINIT_FUNC
PyInit__fineweb_alnum_probe(void)
{
    return PyModule_Create(&probe_module);
}
