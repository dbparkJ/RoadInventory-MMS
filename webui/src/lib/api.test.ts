import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, errorMessageFromPayload } from './api'

describe('point response body lifetime', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  function delayedBodies() {
    const bodies: ReadableStreamDefaultController<Uint8Array>[] = []
    const signals: AbortSignal[] = []
    const responses: Response[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, options) => {
      const signal = options!.signal!
      if (signal.aborted) throw signal.reason
      signals.push(signal)
      const response = new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          bodies.push(controller)
          signal.addEventListener('abort', () => controller.error(signal.reason), { once: true })
        },
      }), { headers: { 'Content-Type': 'application/vnd.mmsp' } })
      responses.push(response)
      return response
    })
    return { bodies, signals, responses }
  }

  async function settleBodyRead(responses: Response[]) {
    await vi.waitFor(() => expect(responses[0]?.body?.locked).toBe(true))
  }

  function closeBodies(bodies: ReadableStreamDefaultController<Uint8Array>[]) {
    for (const body of bodies) {
      try { body.close() } catch { /* An aborted stream is already closed. */ }
    }
  }

  it('cancels a point transfer when its frame changes after response headers', async () => {
    const { bodies, signals, responses } = delayedBodies()
    const controller = new AbortController()
    const pending = api.points('dataset', 'frame', 250_000, controller.signal)
    const outcome = pending.then(() => null, (error: unknown) => error)
    try {
      await settleBodyRead(responses)
      controller.abort()
      expect(signals[0].aborted).toBe(true)
      expect(await outcome).toMatchObject({ name: 'AbortError' })
      expect(fetch).toHaveBeenCalledTimes(1)
    } finally {
      closeBodies(bodies)
      await outcome
    }
  })

  it('keeps the point timeout active through stalled bodies and bounds retries', async () => {
    vi.useFakeTimers()
    const { bodies, signals, responses } = delayedBodies()
    const pending = api.points('dataset', 'frame', 250_000)
    const outcome = pending.then(() => null, (error: unknown) => error)
    try {
      await settleBodyRead(responses)
      await vi.advanceTimersByTimeAsync(120_000)
      expect(signals[0].aborted).toBe(true)
      await vi.runAllTimersAsync()
      expect(await outcome).toMatchObject({ code: 'NETWORK_ERROR', message: '서버 응답 시간이 초과되었습니다.' })
      expect(fetch).toHaveBeenCalledTimes(3)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      closeBodies(bodies)
      await outcome
    }
  })

  it('preserves binary bytes and releases timers and caller listeners on completion', async () => {
    vi.useFakeTimers()
    const { bodies, signals, responses } = delayedBodies()
    const controller = new AbortController()
    const removed = vi.spyOn(controller.signal, 'removeEventListener')
    const pending = api.points('dataset', 'frame', 250_000, controller.signal)
    const bytes = Uint8Array.of(77, 77, 83, 80, 0, 127, 255)
    try {
      await settleBodyRead(responses)
      bodies[0].enqueue(bytes)
      bodies[0].close()
      expect(new Uint8Array(await pending)).toEqual(bytes)
      expect(removed).toHaveBeenCalledWith('abort', expect.any(Function))
      expect(vi.getTimerCount()).toBe(0)
      controller.abort()
      expect(signals[0].aborted).toBe(false)
    } finally {
      closeBodies(bodies)
      await pending.catch(() => undefined)
    }
  })

  it('releases timers and caller listeners when the response is still indexing', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const removed = vi.spyOn(controller.signal, 'removeEventListener')
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ message: '포인트 인덱스 준비 중' }),
      { status: 202, headers: { 'Content-Type': 'application/json' } },
    ))
    await expect(api.points('dataset', 'frame', 250_000, controller.signal))
      .rejects.toMatchObject({ status: 202, code: 'INDEXING' })
    expect(removed).toHaveBeenCalledWith('abort', expect.any(Function))
    expect(vi.getTimerCount()).toBe(0)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('cancels retry backoff without starting another point request', async () => {
    vi.useFakeTimers()
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Synthetic connection failure'))
    const controller = new AbortController()
    let settled = false
    const outcome = api.points('dataset', 'frame', 250_000, controller.signal)
      .then(() => null, (error: unknown) => error)
      .then((result) => { settled = true; return result })
    try {
      await vi.advanceTimersByTimeAsync(0)
      controller.abort()
      await vi.advanceTimersByTimeAsync(0)
      expect(settled).toBe(true)
      expect(await outcome).toMatchObject({ name: 'AbortError' })
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      await vi.runAllTimersAsync()
      await outcome
    }
  })
})

describe('errorMessageFromPayload', () => {
  it('renders FastAPI validation arrays as actionable field messages', () => {
    expect(
      errorMessageFromPayload(
        {
          detail: [
            { loc: ['body', 'parameters', 'confidence'], msg: 'Input should be less than or equal to 1' },
            { loc: ['body', 'track_ids'], msg: 'Field required' },
          ],
        },
        'fallback',
      ),
    ).toBe(
      'parameters.confidence: Input should be less than or equal to 1 · track_ids: Field required',
    )
  })

  it('keeps the server message ahead of nested details', () => {
    expect(
      errorMessageFromPayload({ message: '업로드 세션이 만료되었습니다.', detail: 'ignored' }, 'fallback'),
    ).toBe('업로드 세션이 만료되었습니다.')
  })
})

describe('overlay feature creation', () => {
  it('posts map coordinates and the optimistic revision to the layer feature collection', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          feature: {
            type: 'Feature',
            id: 'f_000000003',
            geometry: { type: 'Point', coordinates: [127, 37] },
            properties: { ID: 3 },
          },
          revision: 2,
          coordinate_space: 'wgs84',
          crs: 'EPSG:4326',
          fields: [{ name: 'ID', type: 'N' }],
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    await api.createOverlayFeature('dataset/a', 'layer 1', {
      geometry: { type: 'Point', coordinates: [127, 37] },
      coordinate_space: 'wgs84',
      expected_revision: 1,
    })

    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/datasets/dataset%2Fa/overlays/layer%201/features')
    expect(options?.method).toBe('POST')
    expect(JSON.parse(String(options?.body))).toEqual({
      geometry: { type: 'Point', coordinates: [127, 37] },
      coordinate_space: 'wgs84',
      expected_revision: 1,
    })

    fetchMock.mockRestore()
  })
})

describe('overlay support feature lookup', () => {
  it('uses the dataset-wide exact support endpoint with an encoded identity', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          dataset_id: 'dataset/a',
          support_id: 'Pole 7/A',
          status: 'not_found',
          items: [],
          count: 0,
          candidate_count: 0,
          scan_complete: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.overlaySupportFeatures('dataset/a', 'Pole 7/A')

      expect(fetchMock).toHaveBeenCalledOnce()
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/overlays/support-features?support_id=Pole+7%2FA',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('overlay layer metadata', () => {
  it('patches the display name and color with an optimistic metadata revision', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          layer: {
            id: 'layer 1',
            dataset_id: 'dataset/a',
            name: '현장 지주',
            color: '#123456',
            metadata_revision: 3,
            geometry_type: 'Point',
            feature_count: 2,
            revision: 1,
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.patchOverlay('dataset/a', 'layer 1', {
        name: '현장 지주',
        color: '#123456',
        expected_metadata_revision: 2,
      })

      expect(fetchMock).toHaveBeenCalledOnce()
      const [url, options] = fetchMock.mock.calls[0]
      expect(url).toBe('/api/datasets/dataset%2Fa/overlays/layer%201')
      expect(options?.method).toBe('PATCH')
      expect(JSON.parse(String(options?.body))).toEqual({
        name: '현장 지주',
        color: '#123456',
        expected_metadata_revision: 2,
      })
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('review workspace API contracts', () => {
  it('uses the P1 session/task URIs and explicit resolution payload', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: [], total: 0, offset: 0, limit: 20, next_offset: null }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            task: {
              id: 'task/1',
              session_id: 'session-1',
              dataset_id: 'dataset/a',
              task_type: 'MANUAL_SCAN',
              status: 'skipped',
              priority: 1,
              frame_id: null,
              track_id: null,
              source_run_id: null,
              source_detection_id: null,
              target_layer_id: null,
              class_hint: null,
              reason_codes: [],
              location_hint: null,
              claimed_by: null,
              resolved_feature_ids: [],
              resolution: 'skipped',
              created_at: '2026-08-24T00:00:00Z',
              updated_at: '2026-08-24T00:00:00Z',
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            frame: {
              id: 'frame 1',
              index: 1,
              track_id: 'Track01',
              timestamp: '2026-08-24T00:00:00Z',
              coordinate: null,
              has_panorama: true,
              has_points: true,
            },
            page_offset: 20,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )

    await api.reviewSessions('dataset/a', 10, 20)
    await api.resolveReviewTask('task/1', { resolution: 'skipped' })
    await api.reviewTaskFrame('dataset/a', 'frame 1')

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/datasets/dataset%2Fa/review-sessions?offset=10&limit=20',
    )
    expect(fetchMock.mock.calls[1][0]).toBe('/api/review-tasks/task%2F1/resolve')
    expect(fetchMock.mock.calls[1][1]?.method).toBe('POST')
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      resolution: 'skipped',
    })
    expect(fetchMock.mock.calls[2][0]).toBe(
      '/api/datasets/dataset%2Fa/frames/frame%201',
    )
  })

  it('encodes task filters and exposes the five report/export download URLs', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ items: [], total: 0, offset: 200, limit: 200, next_offset: null }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.reviewTasks('session/a', 200, 200, undefined, {
        status: 'corrected',
        task_type: 'POLE_BASE_REVIEW',
      })
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/review-sessions/session%2Fa/tasks?offset=200&limit=200&status=corrected&task_type=POLE_BASE_REVIEW',
      )
      expect(api.reviewReportUrl('session/a', 'json')).toBe(
        '/api/review-sessions/session%2Fa/report?format=json',
      )
      expect(api.reviewReportUrl('session/a', 'csv')).toBe(
        '/api/review-sessions/session%2Fa/report?format=csv',
      )
      expect(api.reviewReportUrl('session/a', 'markdown')).toBe(
        '/api/review-sessions/session%2Fa/report?format=markdown',
      )
      expect(api.reviewExportUrl('session/a')).toBe(
        '/api/review-sessions/session%2Fa/export',
      )
      expect(api.reviewActiveLearningExportUrl('session/a')).toBe(
        '/api/review-sessions/session%2Fa/active-learning-export',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('uses bounded QA paging and the exact issue override contract', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [], total: 401, offset: 200, limit: 200, next_offset: 400,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ issue: {} }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))

    try {
      await api.qaIssues('session/a', { offset: 200, limit: 200, status: 'open', severity: 'error' })
      await api.patchQaIssue('issue/a', { status: 'resolved' })
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/review-sessions/session%2Fa/qa/issues?offset=200&limit=200&status=open&severity=error',
      )
      expect(fetchMock.mock.calls[1][0]).toBe('/api/qa/issues/issue%2Fa')
      expect(fetchMock.mock.calls[1][1]?.method).toBe('PATCH')
      expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ status: 'resolved' })
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('encodes the lightweight review completion-status endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        session_status: 'active',
        requirements_met: false,
        can_complete: false,
        blockers: { qa_not_run: 1 },
        checked_at: '2026-08-24T00:00:00Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    try {
      await api.reviewCompletionStatus('session/a')
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/review-sessions/session%2Fa/completion-status',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('preserves nested duplicate reason codes from transactional conflicts', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            message: 'A nearby manual pole requires confirmation.',
            reason_code: 'DUPLICATE_NEARBY',
          },
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      let caught: unknown
      try {
        await api.createOverlayFeature('dataset-a', 'layer-a', {
          geometry: { type: 'Point', coordinates: [1, 2, 3] },
          coordinate_space: 'dataset',
          expected_revision: 1,
        })
      } catch (reason) {
        caught = reason
      }
      expect(caught).toBeInstanceOf(ApiError)
      expect(caught).toMatchObject({
        status: 409,
        code: 'DUPLICATE_NEARBY',
      })
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('manual object duplicate preflight', () => {
  it('passes the edited feature exclusion through the advisory request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          exact_duplicate: false,
          blocked: false,
          candidates: [],
          warning_count: 0,
          radius_m: 0.5,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.duplicateManualObjectPreflight('dataset/a', {
        target_layer_id: 'layer 1',
        template_id: 'SIGN_SUPPORT_POLE',
        position: [1, 2, 3],
        exclude_feature_id: 'feature/7',
      })
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/manual-objects/duplicate-preflight',
      )
      expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
        exclude_feature_id: 'feature/7',
      })
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('overlay attribute schema', () => {
  it('deletes an encoded field name with the optimistic layer revision', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          deleted_field: '상태 값',
          revision: 5,
          fields: [{ name: 'NAME', type: 'C' }],
          layer: {
            id: 'layer 1',
            dataset_id: 'dataset/a',
            name: 'layer',
            geometry_type: 'Point',
            feature_count: 1,
            revision: 5,
          },
          source_preserved: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.deleteOverlayField('dataset/a', 'layer 1', '상태 값', 4)
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/overlays/layer%201/fields/%EC%83%81%ED%83%9C%20%EA%B0%92?expected_revision=4',
      )
      expect(fetchMock.mock.calls[0][1]?.method).toBe('DELETE')
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('run API', () => {
  it('requests a bounded legacy run page for compatibility lookup', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    try {
      await api.runs(undefined, 200)
      expect(fetchMock.mock.calls[0][0]).toBe('/api/runs?limit=200')
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('requests the durable latest completed run for one encoded dataset', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ run: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    try {
      await expect(api.latestCompletedRun('dataset/a ?')).resolves.toEqual({ run: null })
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa%20%3F/runs/latest-completed',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('requests a deterministic completed-run history page for one dataset', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, next_offset: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    try {
      await api.completedRuns('dataset/a', undefined, 200, 400, '2026-08-14T01:02:03+00:00')
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/runs/completed?limit=200&offset=400&snapshot_at=2026-08-14T01%3A02%3A03%2B00%3A00',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('encodes the run id and dismisses it with DELETE', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          id: 'run/a ?',
          dismissed: true,
          artifacts_preserved: true,
          detail: 'preserved',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await expect(api.deleteRun('run/a ?')).resolves.toMatchObject({
        dismissed: true,
        artifacts_preserved: true,
      })
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/runs/run%2Fa%20%3F',
        expect.objectContaining({ method: 'DELETE' }),
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('loads detection models and renames a run with its optimistic timestamp', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        items: [{ id: 'sign.pt', name: 'sign.pt', label: 'sign' }],
        default_model_ids: ['sign.pt'],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        id: 'run/a',
        name: '교통표지 검출',
        dataset_id: 'dataset-a',
        status: 'completed',
        progress: 100,
        created_at: '2026-08-20T00:00:00Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

    try {
      await api.detectionModels()
      await api.renameRun('run/a', {
        name: '교통표지 검출',
        expected_updated_at: '2026-08-20T00:05:00Z',
      })
      expect(fetchMock.mock.calls[0][0]).toBe('/api/detection-models')
      expect(fetchMock.mock.calls[1][0]).toBe('/api/runs/run%2Fa')
      expect(fetchMock.mock.calls[1][1]?.method).toBe('PATCH')
      expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
        name: '교통표지 검출',
        expected_updated_at: '2026-08-20T00:05:00Z',
      })
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('field survey API', () => {
  it('creates and deletes an encoded persistent survey segment', async () => {
    const segment = {
      id: 'survey/a',
      dataset_id: 'dataset/a',
      name: '현장조사 필요구간 1',
      color: '#f59e0b',
      geometry: { type: 'LineString', coordinates: [[127, 37], [127.1, 37.1]] },
      created_at: '2026-08-14T00:00:00Z',
      updated_at: '2026-08-14T00:00:00Z',
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({ segment }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: segment.id, deleted: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))

    try {
      await api.createSurveySegment('dataset/a', {
        name: segment.name,
        color: segment.color,
        coordinates: [[127, 37], [127.1, 37.1]],
      })
      await api.deleteSurveySegment('dataset/a', 'survey/a')
      expect(fetchMock.mock.calls[0][0]).toBe('/api/datasets/dataset%2Fa/survey-segments')
      expect(fetchMock.mock.calls[0][1]?.method).toBe('POST')
      expect(fetchMock.mock.calls[1][0]).toBe(
        '/api/datasets/dataset%2Fa/survey-segments/survey%2Fa',
      )
      expect(fetchMock.mock.calls[1][1]?.method).toBe('DELETE')
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('frame detections API', () => {
  it('requests YOLO boxes by dataset and frame without an SHP layer id', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          dataset_id: 'dataset/a',
          frame_id: 'frame 1',
          coordinate_space: 'panorama_equirectangular_pixels',
          projection: 'equirectangular',
          items: [],
          count: 0,
          model_count: 2,
          truncated: false,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.frameDetections('dataset/a', 'frame 1')
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/frames/frame%201/detections',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })
})

describe('point preview API', () => {
  it('requests a budget and an explicit server-derived color mode', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new ArrayBuffer(40), {
        status: 200,
        headers: { 'Content-Type': 'application/vnd.mmsp' },
      }),
    )

    try {
      await api.points('dataset/a', 'frame 1', 120_000, undefined, 'classification')
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/points/frame%201?budget=120000&color_mode=classification',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('requests ordered dataset-coordinate focus points without changing the legacy call', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new ArrayBuffer(40), {
        status: 200,
        headers: { 'Content-Type': 'application/vnd.mmsp' },
      }),
    )

    try {
      await api.points(
        'dataset/a',
        'frame 1',
        250_000,
        undefined,
        'rgb',
        [
          [209123.456, 412345.678],
          [209124.25, 412346.5],
        ],
      )
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/points/frame%201?budget=250000&color_mode=rgb&focus=209123.456%2C412345.678&focus=209124.25%2C412346.5',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })

  it('resolves an observation independently of loaded overlay pages', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          source_id: 'det-src_0123456789abcdef0123456789abcdef',
          observation_id: 'Dabc:1',
          status: 'not_found',
          match: null,
          candidates: [],
          candidate_count: 0,
          scan_complete: true,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )

    try {
      await api.detectionOverlayFeature(
        'dataset/a',
        'det-src_0123456789abcdef0123456789abcdef',
        'Dabc:1',
      )
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/detections/overlay-feature?source_id=det-src_0123456789abcdef0123456789abcdef&observation_id=Dabc%3A1',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })
})
