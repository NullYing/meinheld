/*
 * Copyright (c) 2009, Cybozu Labs, Inc.
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 * * Redistributions of source code must retain the above copyright notice,
 *   this list of conditions and the following disclaimer.
 * * Redistributions in binary form must reproduce the above copyright notice,
 *   this list of conditions and the following disclaimer in the documentation
 *   and/or other materials provided with the distribution.
 * * Neither the name of the <ORGANIZATION> nor the names of its contributors
 *   may be used to endorse or promote products derived from this software
 *   without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 */

#include <Python.h>
#include <errno.h>
#include <sys/epoll.h>
#include <unistd.h>

#include "picoev.h"
#include "time_cache.h"

#ifndef PICOEV_EPOLL_DEFER_DELETES
#define PICOEV_EPOLL_DEFER_DELETES 1
#endif

/* EPOLLEXCLUSIVE was added in Linux 4.5, but may be supported by a
 * backported kernel even when the userspace headers do not define it. */
#ifndef EPOLLEXCLUSIVE
#define EPOLLEXCLUSIVE (1 << 28)
#endif

typedef struct picoev_loop_epoll_st {
  picoev_loop loop;
  int epfd;
  struct epoll_event events[1024];
} picoev_loop_epoll;

picoev_globals picoev;

picoev_loop* picoev_create_loop(int max_timeout) {
  picoev_loop_epoll* loop;

  /* init parent */
  assert(PICOEV_IS_INITED);
  if ((loop = (picoev_loop_epoll*)malloc(sizeof(picoev_loop_epoll))) == NULL) {
    return NULL;
  }
  if (picoev_init_loop_internal(&loop->loop, max_timeout) != 0) {
    free(loop);
    return NULL;
  }

  /* init myself */
  if ((loop->epfd = epoll_create(picoev.max_fd)) == -1) {
    picoev_deinit_loop_internal(&loop->loop);
    free(loop);
    return NULL;
  }

  loop->loop.now = current_msec / 1000;
  return &loop->loop;
}

int picoev_destroy_loop(picoev_loop* _loop) {
  picoev_loop_epoll* loop = (picoev_loop_epoll*)_loop;

  if (close(loop->epfd) != 0) {
    return -1;
  }
  picoev_deinit_loop_internal(&loop->loop);
  free(loop);
  return 0;
}

static int picoev_epoll_add(picoev_loop_epoll* loop, int fd,
                            struct epoll_event* ev, int exclusive) {
  int ret;
  if (exclusive) {
    ev->events |= EPOLLEXCLUSIVE;
  }
  ret = epoll_ctl(loop->epfd, EPOLL_CTL_ADD, fd, ev);
  if (ret != 0 && exclusive && errno == EINVAL) {
    /* Older kernels may reject this flag instead of ignoring it. */
    ev->events &= ~EPOLLEXCLUSIVE;
    ret = epoll_ctl(loop->epfd, EPOLL_CTL_ADD, fd, ev);
  }
  return ret;
}

int picoev_update_events_internal(picoev_loop* _loop, int fd, int events) {
  picoev_loop_epoll* loop = (picoev_loop_epoll*)_loop;
  picoev_fd* target = picoev.fds + fd;
  struct epoll_event ev;
  int old_events = target->events;
  int new_events = events & PICOEV_READWRITE;
  int exclusive = (events & PICOEV_ADD) != 0
                      ? events & PICOEV_EXCLUSIVE
                      : old_events & PICOEV_EXCLUSIVE;

  assert(PICOEV_FD_BELONGS_TO_LOOP(&loop->loop, fd));

  if (new_events != 0) {
    new_events |= exclusive;
  }
  if (unlikely(new_events == old_events && (events & PICOEV_DEL) == 0)) {
    return 0;
  }

  ev.events = ((events & PICOEV_READ) != 0 ? EPOLLIN : 0) |
              ((events & PICOEV_WRITE) != 0 ? EPOLLOUT : 0);
  ev.data.fd = fd;

#if PICOEV_EPOLL_DEFER_DELETES
  if ((events & PICOEV_DEL) != 0 &&
      (old_events & PICOEV_EXCLUSIVE) == 0) {
    /* Ordinary descriptors can retain their registration for reuse. */
    target->events = 0;
    return 0;
  }
#endif

  if ((old_events & PICOEV_EXCLUSIVE) != 0) {
    /* The kernel forbids MOD on an EPOLLEXCLUSIVE registration. Delete it
     * synchronously, including before a timeout-only shutdown registration. */
    if (epoll_ctl(loop->epfd, EPOLL_CTL_DEL, fd, &ev) != 0) {
      return -1;
    }
    target->events = 0;
    old_events = 0;
  }

  if ((new_events & PICOEV_READWRITE) == 0) {
    if ((old_events & PICOEV_READWRITE) != 0 &&
        epoll_ctl(loop->epfd, EPOLL_CTL_DEL, fd, &ev) != 0) {
      return -1;
    }
  } else if (exclusive) {
    if (picoev_epoll_add(loop, fd, &ev, 1) != 0) {
      return -1;
    }
  } else {
#if !PICOEV_EPOLL_DEFER_DELETES
    if (old_events == 0) {
      if (picoev_epoll_add(loop, fd, &ev, 0) != 0) {
        return -1;
      }
    } else
#endif
    {
      if (epoll_ctl(loop->epfd, EPOLL_CTL_MOD, fd, &ev) != 0 &&
          (errno != ENOENT || picoev_epoll_add(loop, fd, &ev, 0) != 0)) {
        return -1;
      }
    }
  }

  target->events = new_events;

  return 0;
}

int picoev_poll_once_internal(picoev_loop* _loop, int max_wait) {
  picoev_loop_epoll* loop = (picoev_loop_epoll*)_loop;
  int i, nevents;

  Py_BEGIN_ALLOW_THREADS nevents = epoll_wait(
      loop->epfd, loop->events, sizeof(loop->events) / sizeof(loop->events[0]),
      max_wait * 1000);
  Py_END_ALLOW_THREADS cache_time_update();

  if (nevents == -1) {
    return -1;
  }
  for (i = 0; likely(i < nevents); ++i) {
    struct epoll_event* event = loop->events + i;
    picoev_fd* target = picoev.fds + event->data.fd;
    if (loop->loop.loop_id == target->loop_id &&
        likely((target->events & PICOEV_READWRITE) != 0)) {
      int revents = ((event->events & EPOLLIN) != 0 ? PICOEV_READ : 0) |
                    ((event->events & EPOLLOUT) != 0 ? PICOEV_WRITE : 0);
      if (likely(revents != 0)) {
        (*target->callback)(&loop->loop, event->data.fd, revents,
                            target->cb_arg);
      }
    } else {
#if PICOEV_EPOLL_DEFER_DELETES
      event->events = 0;
      if (epoll_ctl(loop->epfd, EPOLL_CTL_DEL, event->data.fd, event) != 0 &&
          errno != ENOENT && errno != EBADF) {
        return -1;
      }
#endif
    }
  }
  return 0;
}
