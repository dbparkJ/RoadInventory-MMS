import { describe, expect, it } from 'vitest'
import type { RunRecord } from './types'
import { completedDetectionRevision } from './App'

const RUN: RunRecord = {
  id: 'run-1',
  dataset_id: 'dataset-1',
  status: 'running',
  progress: 90,
  created_at: '2026-08-13T01:00:00Z',
}

describe('completedDetectionRevision', () => {
  it('changes when the active dataset run completes and ignores other datasets', () => {
    expect(completedDetectionRevision([RUN], 'dataset-1')).toBe('')
    expect(completedDetectionRevision([
      {
        ...RUN,
        status: 'completed',
        progress: 100,
        finished_at: '2026-08-13T01:05:00Z',
      },
      {
        ...RUN,
        id: 'run-other',
        dataset_id: 'dataset-2',
        status: 'completed',
      },
    ], 'dataset-1')).toBe(
      '2026-08-13T01:00:00Z:run-1:2026-08-13T01:05:00Z',
    )
  })
})
