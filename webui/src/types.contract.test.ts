import { describe, expect, it } from 'vitest'
import type {
  BootstrapResponse,
  FeatureProvenance,
  GeometryProposal,
  ManualObservation,
  QaIssue,
  ReviewSession,
  ReviewTask,
} from './types'

const reviewSession = {
  id: 'rvw_1',
  dataset_id: 'ds_1',
  source_run_ids: ['run_1'],
  target_layer_ids: ['ov_1'],
  track_ids: ['Track01'],
  frame_range: [0, 1200],
  class_filters: ['TRAFFIC_SIGN', 'SIGN_SUPPORT_POLE'],
  status: 'active',
  created_by: 'operator-local',
  created_at: '2026-08-24T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
  last_task_id: 'rvt_1',
  qa_layer_revisions: null,
  qa_ran_at: null,
} satisfies ReviewSession

const reviewTask = {
  id: 'rvt_1',
  session_id: reviewSession.id,
  dataset_id: reviewSession.dataset_id,
  task_type: 'PROJECTION_FAILED',
  status: 'todo',
  priority: 72,
  frame_id: 'frm_1',
  track_id: 'Track01',
  frame_start: null,
  frame_end: null,
  source_run_id: 'run_1',
  source_detection_id: 'det_1',
  target_layer_id: 'ov_1',
  class_hint: 'TRAFFIC_SIGN',
  reason_codes: ['NO_SUPPORTING_POINTS'],
  location_hint: [123, 456, 10],
  claimed_by: null,
  resolved_feature_ids: [],
  resolution: null,
  created_at: '2026-08-24T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
} satisfies ReviewTask

const manualObservation = {
  observation_id: 'mob_1',
  dataset_id: reviewSession.dataset_id,
  frame_id: 'frm_1',
  view_type: 'panorama',
  class_name: 'TRAFFIC_SIGN',
  geometry_2d: {
    type: 'equirectangular_bbox',
    u_intervals: [
      [0.94, 1],
      [0, 0.03],
    ],
    v_min: 0.22,
    v_max: 0.41,
    image_width: 7040,
    image_height: 3520,
  },
  created_by: 'operator-local',
} satisfies ManualObservation

const geometryProposal = {
  proposal_id: 'prp_1',
  tool_id: 'panorama_bbox_point_v1',
  status: 'review',
  coordinate_space: 'dataset',
  geometry: { type: 'Point', coordinates: [123, 456, 7.8] },
  property_patch: { CLASS_NM: 'TRAFFIC_SIGN' },
  quality: {
    score: 0.78,
    support_point_count: 43,
    depth_spread_m: 0.18,
    reprojection_error_px: 4.2,
  },
  reason_codes: ['DEPTH_CLUSTER_WEAK'],
  evidence: {
    frame_id: 'frm_1',
    observation_id: manualObservation.observation_id,
    seed_position: [123.1, 456, 8.2],
  },
} satisfies GeometryProposal

const featureProvenance = {
  layer_id: 'ov_1',
  feature_id: 'f_000000123',
  origin: 'MANUAL',
  source_run_id: 'run_1',
  source_frame_ids: ['frm_1'],
  source_detection_ids: [],
  manual_observation_ids: [manualObservation.observation_id],
  creation_tool: geometryProposal.tool_id,
  proposal_quality: geometryProposal.quality.score,
  review_status: 'confirmed',
  created_by: 'operator-local',
  created_at: '2026-08-24T00:00:00Z',
  updated_at: '2026-08-24T00:00:00Z',
} satisfies FeatureProvenance

const qaIssue = {
  id: 'qai_1',
  session_id: reviewSession.id,
  layer_id: featureProvenance.layer_id,
  feature_id: featureProvenance.feature_id,
  rule_id: 'DUPLICATE_NEARBY',
  severity: 'warning',
  message: '0.31m 안에 같은 클래스 객체가 있습니다.',
  related_feature_ids: ['f_000000087'],
  status: 'open',
} satisfies QaIssue

const capabilities = {
  review_workspace: false,
  active_learning_export: false,
} satisfies NonNullable<BootstrapResponse['capabilities']>

describe('P1 review workspace draft contracts', () => {
  it('keeps seam-aware observations, proposals, provenance, QA, and the disabled capability representable', () => {
    expect(reviewTask.session_id).toBe(reviewSession.id)
    expect(manualObservation.geometry_2d.u_intervals).toHaveLength(2)
    expect(geometryProposal.evidence.observation_id).toBe(manualObservation.observation_id)
    expect(qaIssue.feature_id).toBe(featureProvenance.feature_id)
    expect(capabilities.review_workspace).toBe(false)
    expect(capabilities.active_learning_export).toBe(false)
  })
})
