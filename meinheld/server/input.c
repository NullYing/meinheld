#include "input.h"

#define IO_MAXFREELIST 1024

static InputObject *io_free_list[IO_MAXFREELIST];
static int io_numfree = 0;

void InputObject_list_clear(void) {
  InputObject *op;

  while (io_numfree) {
    op = io_free_list[--io_numfree];
    PyObject_DEL(op);
  }
}

static InputObject *alloc_InputObject(void) {
  InputObject *io;
  if (io_numfree) {
    io = io_free_list[--io_numfree];
    PyObject_Init((PyObject *)io, &InputObjectType);
    // DEBUG("use pooled StringIOObject %p", io);
  } else {
    io = PyObject_NEW(InputObject, &InputObjectType);
    // DEBUG("alloc StringIOObject %p", io);
  }
  return io;
}

static void dealloc_InputObject(InputObject *io) {
  if (io->buffer) {
    free_buffer(io->buffer);
    io->buffer = NULL;
  }
  if (io_numfree < IO_MAXFREELIST) {
    // DEBUG("back to StringIOObject pool %p\n", io);
    io_free_list[io_numfree++] = io;
  } else {
    PyObject_DEL(io);
  }
}

int Check_InputObject(PyObject *obj) {
  if (obj->ob_type != &InputObjectType) {
    return 0;
  }
  return 1;
}

PyObject *InputObject_New(buffer_t *buf) {
  InputObject *io;
  io = alloc_InputObject();
  if (io == NULL) {
    return NULL;
  }
  io->buffer = buf;
  io->pos = 0;
  return (PyObject *)io;
}

void InputObject_dealloc(InputObject *self) {
  if (self->buffer) {
    free_buffer(self->buffer);
    self->buffer = NULL;
  }
  dealloc_InputObject(self);
}

static int is_close(InputObject *self) {
  if (self->buffer == NULL) {
    PyErr_SetString(PyExc_IOError, "closed");
    return 1;
  }
  return 0;
}

static PyObject *InputObject_read(InputObject *self, PyObject *args) {
  Py_ssize_t n = -1, l = 0;
  PyObject *s;

  if (!PyArg_ParseTuple(args, "|n:read", &n)) {
    return NULL;
  }

  if (is_close(self)) {
    return NULL;
  }
  l = self->buffer->len - self->pos;
  if (n < 0 || n > l) {
    n = l;
    if (n < 0) {
      n = 0;
    }
  }
  s = PyBytes_FromStringAndSize(self->buffer->buf + self->pos, n);
  if (!s) {
    return NULL;
  }
  self->pos += n;
  return s;
}

static Py_ssize_t inner_readline(InputObject *self, Py_ssize_t size,
                                char **output) {
  char *start = self->buffer->buf + self->pos;
  char *newline;
  Py_ssize_t l = (Py_ssize_t)self->buffer->len - self->pos;

  if (size >= 0 && size < l) {
    l = size;
  }
  newline = memchr(start, '\n', (size_t)l);
  if (newline != NULL) {
    l = newline - start + 1;
  }
  *output = start;
  self->pos += l;
  return l;
}

static PyObject *InputObject_readline(InputObject *self, PyObject *args) {
  Py_ssize_t len, size = -1;
  char *output;

  if (args) {
    if (!PyArg_ParseTuple(args, "|n:readline", &size)) {
      return NULL;
    }
  }
  if (is_close(self)) {
    return NULL;
  }

  len = inner_readline(self, size, &output);
  return PyBytes_FromStringAndSize(output, len);
}

static PyObject *InputObject_readlines(InputObject *self, PyObject *args) {
  Py_ssize_t len;
  char *output;
  PyObject *result, *new_line;
  Py_ssize_t sizehint = 0, length = 0;

  if (!PyArg_ParseTuple(args, "|n:readlines", &sizehint)) {
    return NULL;
  }
  if (is_close(self)) {
    return NULL;
  }

  result = PyList_New(0);
  if (!result) {
    return NULL;
  }

  while (1) {
    len = inner_readline(self, -1, &output);
    if (len == 0) {
      break;
    }
    new_line = PyBytes_FromStringAndSize(output, len);
    if (!new_line) {
      goto err;
    }
    if (PyList_Append(result, new_line) == -1) {
      Py_DECREF(new_line);
      goto err;
    }
    Py_DECREF(new_line);
    length += len;
    if (sizehint > 0 && length >= sizehint) {
      break;
    }
  }
  return result;
err:
  Py_DECREF(result);
  return NULL;
}

static PyObject *InputObject_iternext(InputObject *self) {
  PyObject *next;
  if (is_close(self)) {
    return NULL;
  }
  next = InputObject_readline(self, NULL);
  if (!next) {
    return NULL;
  }
  if (!PyBytes_GET_SIZE(next)) {
    Py_DECREF(next);
    PyErr_SetNone(PyExc_StopIteration);
    return NULL;
  }
  return next;
}

static struct PyMethodDef InputObject_methods[] = {
    {"read", (PyCFunction)InputObject_read, METH_VARARGS, ""},
    {"readline", (PyCFunction)InputObject_readline, METH_VARARGS, ""},
    {"readlines", (PyCFunction)InputObject_readlines, METH_VARARGS, ""},
    {NULL, NULL}};

PyTypeObject InputObjectType = {
#ifdef PY3
    PyVarObject_HEAD_INIT(NULL, 0)
#else
    PyObject_HEAD_INIT(NULL) 0, /* ob_size */
#endif
        "meinheld.input",               /*tp_name*/
    sizeof(InputObject),                /*tp_basicsize*/
    0,                                  /*tp_itemsize*/
    (destructor)InputObject_dealloc,    /*tp_dealloc*/
    0,                                  /*tp_print*/
    0,                                  /*tp_getattr*/
    0,                                  /*tp_setattr*/
    0,                                  /*tp_compare*/
    0,                                  /*tp_repr*/
    0,                                  /*tp_as_number*/
    0,                                  /*tp_as_sequence*/
    0,                                  /*tp_as_mapping*/
    0,                                  /*tp_hash */
    0,                                  /*tp_call*/
    0,                                  /*tp_str*/
    0,                                  /*tp_getattro*/
    0,                                  /*tp_setattro*/
    0,                                  /*tp_as_buffer*/
    Py_TPFLAGS_DEFAULT,                 /*tp_flags*/
    "Input",                            /* tp_doc */
    0,                                  /* tp_traverse */
    0,                                  /* tp_clear */
    0,                                  /* tp_richcompare */
    0,                                  /* tp_weaklistoffset */
    PyObject_SelfIter,                  /*tp_iter */
    (iternextfunc)InputObject_iternext, /* tp_iternext */
    InputObject_methods,                /* tp_methods */
    0,                                  /* tp_members */
    0,                                  /* tp_getset */
    0,                                  /* tp_base */
    0,                                  /* tp_dict */
    0,                                  /* tp_descr_get */
    0,                                  /* tp_descr_set */
    0,                                  /* tp_dictoffset */
    0,                                  /* tp_init */
    0,                                  /* tp_alloc */
    0,                                  /* tp_new */
};
