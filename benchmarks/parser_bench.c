/* Standalone benchmark helper; never part of the meinheld server extension. */
#include <Python.h>
#include <time.h>
#include "http_request_parser.h"
#include "request.h"

static PyObject *bench(PyObject *self, PyObject *args) {
  int iterations, chunk;
  const char *wire;
  Py_ssize_t length;
  if (!PyArg_ParseTuple(args, "y#ii", &wire, &length, &iterations, &chunk)) return NULL;
  if (chunk <= 0) chunk = length;
  setup_static_env("localhost", 8000);
  if (PyErr_Occurred()) return NULL;
  clock_t start = clock();
  for (int i = 0; i < iterations; i++) {
    client_t client = {0};
    client.fd = -1;
    client.remote_addr = "127.0.0.1";
    client.remote_port = 12345;
    client.request_queue = new_request_queue();
    if (client.request_queue == NULL || init_parser(&client, "localhost", 8000) < 0) {
      clear_static_env();
      return PyErr_NoMemory();
    }
    int failed = 0;
    for (Py_ssize_t offset = 0; offset < length; offset += chunk) {
      size_t take = length - offset < chunk ? length - offset : chunk;
      if (execute_parse(&client, wire + offset, take) != take) {
        failed = 1;
        break;
      }
    }
    request *req = client.request_queue->head;
    if (req) Py_CLEAR(req->environ);
    free_request_queue(client.request_queue);
    dealloc_parser(client.http_parser);
    if (failed) {
      clear_static_env();
      if (!PyErr_Occurred()) PyErr_SetString(PyExc_RuntimeError, "parser rejected benchmark request");
      return NULL;
    }
  }
  double elapsed = (double)(clock() - start) / CLOCKS_PER_SEC;
  clear_static_env();
  return PyFloat_FromDouble(elapsed * 1e6 / iterations);
}

static PyMethodDef methods[] = {{"bench", bench, METH_VARARGS, NULL}, {NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_parser_bench", NULL, -1, methods};
PyMODINIT_FUNC PyInit__parser_bench(void) { return PyModule_Create(&module); }
