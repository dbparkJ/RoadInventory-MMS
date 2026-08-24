export const POINT_CLOUD_DETECTION_FOCUS_EVENT = 'mms-pointcloud-focus-detection'

export interface PointCloudDetectionFocusEventDetail {
  datasetId: string
  frameId: string
  sourceId: string
  observationId: string
}

export function dispatchPointCloudDetectionFocus(
  detail: PointCloudDetectionFocusEventDetail,
  target: EventTarget = window,
) {
  target.dispatchEvent(new CustomEvent(POINT_CLOUD_DETECTION_FOCUS_EVENT, { detail }))
}
