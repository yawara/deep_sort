# vim: expandtab:ts=4:sw=4
from __future__ import absolute_import
import numpy as np
from numpy import linalg as LA
from . import kalman_filter
from . import linear_assignment
from . import iou_matching
from .track import Track
from cython_bbox import bbox_overlaps


def has_same_subfeat(det, tracks, feat_thresh=0.9, iou_thresh=0.5):
    if det.sub_feature is None:
        return False

    tracks = [
        track for track in tracks if track.detection is not None and track.detection.sub_feature is not None]
    if len(tracks) == 0:
        return False
    t_tlbrs = [track.detection.sub_tlbr for track in tracks]
    d_tlbrs = [det.sub_tlbr]
    ious = bbox_overlaps(
        np.ascontiguousarray(d_tlbrs, dtype=float),
        np.ascontiguousarray(t_tlbrs, dtype=float),
    )
    for track_id, iou in enumerate(ious[0]):
        if iou > iou_thresh:
            dist = LA.norm(det.sub_feature -
                           tracks[track_id].detection.sub_feature)
            if dist < feat_thresh:
                return True

    return False


class Tracker:
    """
    This is the multi-target tracker.

    Parameters
    ----------
    metric : nn_matching.NearestNeighborDistanceMetric
        A distance metric for measurement-to-track association.
    max_age : int
        Maximum number of missed misses before a track is deleted.
    n_init : int
        Number of consecutive detections before the track is confirmed. The
        track state is set to `Deleted` if a miss occurs within the first
        `n_init` frames.

    Attributes
    ----------
    metric : nn_matching.NearestNeighborDistanceMetric
        The distance metric used for measurement to track association.
    max_age : int
        Maximum number of missed misses before a track is deleted.
    n_init : int
        Number of frames that a track remains in initialization phase.
    kf : kalman_filter.KalmanFilter
        A Kalman filter to filter target trajectories in image space.
    tracks : List[Track]
        The list of active tracks at the current time step.

    """

    def __init__(self, metric, sub_metric, max_iou_distance=0.7, max_age=30, n_init=3):
        self.metric = metric
        self.sub_metric = sub_metric
        self.max_iou_distance = max_iou_distance
        self.max_age = max_age
        self.n_init = n_init

        self.kf = kalman_filter.KalmanFilter()
        self.tracks = []
        self._next_id = 1

    def predict(self):
        """Propagate track state distributions one time step forward.

        This function should be called once every time step, before `update`.
        """
        for track in self.tracks:
            track.predict(self.kf)

    def update(self, detections):
        """Perform measurement update and track management.

        Parameters
        ----------
        detections : List[deep_sort.detection.Detection]
            A list of detections at the current time step.

        """
        # Run matching cascade.
        matches, unmatched_tracks, unmatched_detections = \
            self._match(detections)

        # Update track set.
        for track_idx, detection_idx in matches:
            self.tracks[track_idx].update(
                self.kf, detections[detection_idx])
        for track_idx in unmatched_tracks:
            self.tracks[track_idx].mark_missed()
        self.tracks = [t for t in self.tracks if not t.is_deleted()]
        for detection_idx in unmatched_detections:
            if has_same_subfeat(detections[detection_idx], self.tracks):
                continue
            self._initiate_track(detections[detection_idx])

        # Update distance metric.
        active_targets = [t.track_id for t in self.tracks if t.is_confirmed()]
        features, targets = [], []
        sub_feats, sub_targets = [], []
        for track in self.tracks:
            if not track.is_confirmed():
                continue
            features += track.features
            targets += [track.track_id for _ in track.features]
            for subfeat in track.sub_features:
                if subfeat is not None:
                    sub_feats.append(subfeat)
                    sub_targets.append(track.track_id)
        self.metric.partial_fit(
            np.asarray(features), np.asarray(targets), active_targets)
        self.sub_metric.partial_fit(
            np.asarray(sub_feats), np.asarray(sub_targets), active_targets
        )

    def _match(self, detections):

        def gated_metric(tracks, dets, track_indices, detection_indices):
            features = np.array([dets[i].feature for i in detection_indices])
            targets = np.array([tracks[i].track_id for i in track_indices])
            cost_matrix = self.metric.distance(features, targets)
            cost_matrix = linear_assignment.gate_cost_matrix(
                self.kf, cost_matrix, tracks, dets, track_indices,
                detection_indices)

            return cost_matrix

        def sub_metric(tracks, dets, track_indices, detection_indices):
            sub_features = np.array(
                [dets[i].sub_feature for i in detection_indices])
            targets = np.array([tracks[i].track_id for i in track_indices])
            cost_matrix = self.sub_metric.distance(sub_features, targets)
            return cost_matrix

        # Split track set into confirmed and unconfirmed tracks.
        confirmed_tracks = [
            i for i, t in enumerate(self.tracks) if t.is_confirmed()]
        unconfirmed_tracks = [
            i for i, t in enumerate(self.tracks) if not t.is_confirmed()]

        # Associate confirmed tracks using appearance features.
        matches_a, unmatched_tracks_a, unmatched_detections = \
            linear_assignment.matching_cascade(
                gated_metric, self.metric.matching_threshold, self.max_age,
                self.tracks, detections, confirmed_tracks)

        # Associate remaining tracks together with unconfirmed tracks using IOU.
        iou_track_candidates = unconfirmed_tracks + [
            k for k in unmatched_tracks_a if
            self.tracks[k].time_since_update == 1]
        unmatched_tracks_a = [
            k for k in unmatched_tracks_a if
            self.tracks[k].time_since_update != 1]
        matches_b, unmatched_tracks_b, unmatched_detections = \
            linear_assignment.min_cost_matching(
                iou_matching.iou_cost, self.max_iou_distance, self.tracks,
                detections, iou_track_candidates, unmatched_detections)

        # Associate remaing tracks using apperance sub-features
        unmatched_tracks_ab = set(unmatched_tracks_a + unmatched_tracks_b)
        subfeat_track_candidates = [
            k for k in unmatched_tracks_ab if self.sub_metric.has_samples(self.tracks[k].track_id)
        ]
        unmatched_tracks_ab = [
            k for k in unmatched_tracks_ab
            if not self.sub_metric.has_samples(self.tracks[k].track_id)
        ]
        subfeat_det_candidates = [
            k for k in unmatched_detections if
            detections[k].sub_feature is not None
        ]
        unmatched_detections_ab = [
            k for k in unmatched_detections if
            detections[k].sub_feature is None
        ]

        matches_c, unmatched_tracks_c, unmatched_detections_c = linear_assignment.min_cost_matching(
            sub_metric, self.sub_metric.matching_threshold, self.tracks, detections,
            subfeat_track_candidates, subfeat_det_candidates
        )

        matches = matches_a + matches_b + matches_c
        unmatched_tracks = unmatched_tracks_ab + unmatched_tracks_c
        unmatched_detections = unmatched_detections_ab + unmatched_detections_c
        return matches, unmatched_tracks, unmatched_detections

    def _initiate_track(self, detection):
        mean, covariance = self.kf.initiate(detection.to_xyah())
        self.tracks.append(Track(
            detection,
            mean, covariance, self._next_id, self.n_init, self.max_age,
            detection.feature, detection.sub_feature
        ))
        self._next_id += 1
