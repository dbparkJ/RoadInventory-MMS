import { describe, expect, it, vi } from 'vitest'
import { api, errorMessageFromPayload } from './api'

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
  it('requests only a budget because the server owns the 15m/25m distance bands', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(new ArrayBuffer(40), {
        status: 200,
        headers: { 'Content-Type': 'application/vnd.mmsp' },
      }),
    )

    try {
      await api.points('dataset/a', 'frame 1', 120_000)
      expect(fetchMock.mock.calls[0][0]).toBe(
        '/api/datasets/dataset%2Fa/points/frame%201?budget=120000',
      )
    } finally {
      fetchMock.mockRestore()
    }
  })
})
