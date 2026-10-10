/* Standalone checks keep kernel errors visible when assertions are disabled. */
#include "../meinheld/server/picoev_epoll.c"

#ifndef PICOEV_EXCLUSIVE
#define PICOEV_EXCLUSIVE 8
#endif

/* The backend releases the GIL around epoll_wait; these checks have no Python
 * interpreter or threads, so they only need the matching no-op hooks. */
PyThreadState* PyEval_SaveThread(void) { return NULL; }
void PyEval_RestoreThread(PyThreadState* state) { (void)state; }

volatile uintptr_t current_msec = 100000;
void cache_time_update(void) {}

static int seen_events;

static void record_events(picoev_loop* loop, int fd, int events, void* arg) {
  (void)loop;
  (void)fd;
  (void)arg;
  seen_events |= events;
}

#define CHECK(condition)                                                    \
  do {                                                                    \
    if (!(condition)) {                                                    \
      fprintf(stderr, "%s:%d: %s (errno=%d)\n", __FILE__, __LINE__,       \
              #condition, errno);                                         \
      return 1;                                                           \
    }                                                                     \
  } while (0)

static int check_read_write(picoev_loop* loop, int fd, int peer, int readd) {
  char byte;
  CHECK(picoev_add(loop, fd, PICOEV_READ, 0, record_events, NULL) == 0);
  CHECK(write(peer, "x", 1) == 1);
  seen_events = 0;
  CHECK(picoev_poll_once_internal(loop, 0) == 0);
  CHECK(seen_events == PICOEV_READ);
  CHECK(read(fd, &byte, 1) == 1);
  if (readd) {
    CHECK(picoev_del(loop, fd) == 0);
    CHECK(picoev_add(loop, fd, PICOEV_WRITE, 0, record_events, NULL) == 0);
  } else {
    CHECK(picoev_set_events(loop, fd, PICOEV_WRITE) == 0);
  }
  seen_events = 0;
  CHECK(picoev_poll_once_internal(loop, 0) == 0);
  CHECK(seen_events == PICOEV_WRITE);
  CHECK(picoev_get_events(loop, fd) == PICOEV_WRITE);
  CHECK(picoev_del(loop, fd) == 0);
  return 0;
}

static int check_exclusive_delete(picoev_loop* loop, int fd, int peer) {
  struct epoll_event event;
  picoev_loop_epoll* backend = (picoev_loop_epoll*)loop;
  CHECK(picoev_add(loop, fd, PICOEV_READ | PICOEV_EXCLUSIVE, 0,
                   record_events, NULL) == 0);
  CHECK(picoev_del(loop, fd) == 0);
  event.events = EPOLLIN;
  event.data.fd = fd;
  errno = 0;
  CHECK(epoll_ctl(backend->epfd, EPOLL_CTL_MOD, fd, &event) == -1);
  CHECK(errno == ENOENT);
  CHECK(picoev_add(loop, fd, PICOEV_TIMEOUT, 1, record_events, NULL) == 0);
  CHECK(write(peer, "x", 1) == 1);
  seen_events = 0;
  CHECK(picoev_poll_once_internal(loop, 0) == 0);
  CHECK(seen_events == 0);
  CHECK(picoev_get_events(loop, fd) == 0);
  CHECK(picoev_del(loop, fd) == 0);
  CHECK(picoev_add(loop, fd, PICOEV_READ | PICOEV_EXCLUSIVE, 0,
                   record_events, NULL) == 0);
  seen_events = 0;
  CHECK(picoev_poll_once_internal(loop, 0) == 0);
  CHECK(seen_events == PICOEV_READ);
  CHECK(picoev_del(loop, fd) == 0);
  return 0;
}

static int check_closed_add(picoev_loop* loop, int fd) {
  CHECK(close(fd) == 0);
  errno = 0;
  CHECK(picoev_add(loop, fd, PICOEV_READ, 0, record_events, NULL) == -1);
  CHECK(errno == EBADF);
  CHECK(!picoev_is_active(loop, fd));
  return 0;
}

static int check_closed_update(picoev_loop* loop, int fd) {
  CHECK(picoev_add(loop, fd, PICOEV_READ, 0, record_events, NULL) == 0);
  CHECK(close(fd) == 0);
  errno = 0;
  CHECK(picoev_set_events(loop, fd, PICOEV_WRITE) == -1);
  CHECK(errno == EBADF);
  CHECK(picoev_get_events(loop, fd) == PICOEV_READ);
  return 0;
}

static int check_exclusive_update(picoev_loop* loop, int fd) {
  CHECK(picoev_add(loop, fd, PICOEV_READ | PICOEV_EXCLUSIVE, 0,
                   record_events, NULL) == 0);
  CHECK(picoev_set_events(loop, fd, PICOEV_WRITE) == 0);
  seen_events = 0;
  CHECK(picoev_poll_once_internal(loop, 0) == 0);
  CHECK(seen_events == PICOEV_WRITE);
  CHECK(picoev_get_events(loop, fd) == PICOEV_WRITE);
  CHECK(picoev_del(loop, fd) == 0);
  return 0;
}

static int check_kernel_rejection(picoev_loop* loop) {
  FILE* file = tmpfile();
  int fd;
  CHECK(file != NULL);
  fd = fileno(file);
  errno = 0;
  CHECK(picoev_add(loop, fd, PICOEV_READ, 0, record_events, NULL) == -1);
  CHECK(errno == EPERM);
  CHECK(!picoev_is_active(loop, fd));
  CHECK(fclose(file) == 0);
  return 0;
}

int main(int argc, char** argv) {
  int sockets[2], result;
  picoev_loop* loop;
  CHECK(argc == 2);
  CHECK(picoev_init(128) == 0);
  loop = picoev_create_loop(60);
  CHECK(loop != NULL);
  CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
  if (strcmp(argv[1], "read-write") == 0) {
    result = check_read_write(loop, sockets[0], sockets[1], 0);
  } else if (strcmp(argv[1], "delete-readd") == 0) {
    result = check_read_write(loop, sockets[0], sockets[1], 1);
  } else if (strcmp(argv[1], "exclusive-delete") == 0) {
    result = check_exclusive_delete(loop, sockets[0], sockets[1]);
  } else if (strcmp(argv[1], "closed-add") == 0) {
    result = check_closed_add(loop, sockets[0]);
  } else if (strcmp(argv[1], "closed-update") == 0) {
    result = check_closed_update(loop, sockets[0]);
  } else if (strcmp(argv[1], "exclusive-update") == 0) {
    result = check_exclusive_update(loop, sockets[0]);
  } else if (strcmp(argv[1], "kernel-rejection") == 0) {
    result = check_kernel_rejection(loop);
  } else {
    fprintf(stderr, "Unknown check: %s\n", argv[1]);
    result = 1;
  }
  close(sockets[0]);
  close(sockets[1]);
  CHECK(picoev_destroy_loop(loop) == 0);
  CHECK(picoev_deinit() == 0);
  return result;
}
