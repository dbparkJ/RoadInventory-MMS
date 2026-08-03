import { describe, expect, it } from 'vitest'
import * as THREE from 'three'
import {
  capturePointCloudViewState,
  datasetPointToFrameLocal,
  restorePointCloudViewState,
} from './PointCloudView'

describe('PointCloudView camera continuity', () => {
  it('restores camera angle, orbit target, and zoom for the next frame', () => {
    const camera = new THREE.PerspectiveCamera(52, 1, 0.05, 2_000)
    const target = new THREE.Vector3(-1.25, 4.5, 2.1)
    camera.position.set(14.2, -18.4, 9.8)
    camera.zoom = 1.7
    const saved = capturePointCloudViewState(camera, target)

    camera.position.set(1, 2, 3)
    camera.zoom = 1
    target.set(0, 0, 0)
    restorePointCloudViewState(camera, target, saved)

    expect(camera.position.toArray()).toEqual([14.2, -18.4, 9.8])
    expect(target.toArray()).toEqual([-1.25, 4.5, 2.1])
    expect(camera.zoom).toBe(1.7)
  })

  it('converts an absolute SHP point into the current MMSP frame coordinates', () => {
    expect(datasetPointToFrameLocal([1007.5, 2012, 31.25], [1000, 2000, 30])).toEqual([
      7.5,
      12,
      1.25,
    ])
    expect(datasetPointToFrameLocal([1007.5, 2012], [1000, 2000, 30])).toEqual([7.5, 12, 0])
  })
})
