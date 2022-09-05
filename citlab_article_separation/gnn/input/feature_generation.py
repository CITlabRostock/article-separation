import os
import json
import re
import time
import numpy as np
from scipy.spatial import Delaunay
from scipy.spatial.qhull import QhullError
from shapely.geometry import LineString
from citlab_python_util.math.rounding import round_by_precision_and_base as round_base
from citlab_python_util.io.path_util import get_img_from_page_path
from citlab_article_separation.gnn.input.textblock_similarity import TextblockSimilarity
from citlab_python_util.image_processing.swt_dist_trafo import StrokeWidthDistanceTransform
from citlab_python_util.parser.xml.page.page import Page
from citlab_python_util.geometry.util import convex_hull, bounding_box
from citlab_python_util.logging.custom_logging import setup_custom_logger

logger = setup_custom_logger(__name__, level="info")


def get_text_region_geometric_features(text_region, norm_x, norm_y):
    """
    Generates 4d geometric features for `text_region`.

    A 2d feature describing the size of the text region, i.e. a vector (w,h) spanning the `text_region` and
    a 2d feature describing the center (x,y) of the `text_region`.

    Each component of the two 2d features is normed by `norm_x` and `norm_y` respectively.

    :param text_region: TextRegion object
    :param norm_x: norm scalar for the width/x (usually image width)
    :param norm_y: norm scalar for the height/y (usually image height)
    :return: 4d geometric feature vector
    """
    tr_points = np.array(text_region.points.points_list, dtype=np.int32)
    # bounding box of text region
    min_x, max_x, min_y, max_y = get_bounding_box(tr_points)
    width = float(max_x) - float(min_x)
    height = float(max_y) - float(min_y)
    # feature vector describing the extension of the text region
    size_x = width / norm_x
    size_y = height / norm_y
    # feature vector describing the center of the text region
    center_x = (min_x + max_x) / (2 * norm_x)
    center_y = (min_y + max_y) / (2 * norm_y)
    # 4-dimensional feature
    return [size_x, size_y, center_x, center_y]


def get_text_region_baseline_features(text_region, norm_x, norm_y):
    """
    Generates 8d baseline features for `text_region`.

    Picks the top and bottom baseline of the text region and computes 4d geometric features for each, describing
    their size and center.

    Features regarding the width or x-coordinate are normed by `norm_x`, whereas features regarding the height
    or y-coordinate are normed by `norm_y`.

    :param text_region: TextRegion object
    :param norm_x: norm scalar for the width/x (usually image width)
    :param norm_y: norm scalar for the height/y (usually image height)
    :return: 8d baseline feature vector
    """
    feature = []
    # geometric information about top & bottom textline of text region
    top_baseline = text_region.text_lines[0].baseline
    bottom_baseline = text_region.text_lines[-1].baseline
    for baseline in (top_baseline, bottom_baseline):
        # bounding box of baseline
        points_baseline = np.asarray(baseline.points_list, dtype=np.int32)
        min_x, max_x, min_y, max_y = get_bounding_box(points_baseline)
        width = float(max_x) - float(min_x)
        height = float(max_y) - float(min_y)
        # feature vector describing the extension of the baseline
        size_x = width / norm_x
        size_y = height / norm_y
        # feature vector describing the center of the baseline
        center_x = (min_x + max_x) / (2 * norm_x)
        center_y = (min_y + max_y) / (2 * norm_y)
        # extend feature
        feature.extend([size_x, size_y, center_x, center_y])
    # return 8-dimensional feature
    return feature


# def get_text_region_punctuation_feature(text_region):
#     # 1-feature for empty text regions
#     if all([not line.text for line in text_region.text_lines]):
#         return [1.0, 1.0]
#     # context information about beginning and end of text region (character based)
#     # shift top textline until it contains text
#     index_top = 0
#     while not text_region.text_lines[index_top].text:
#         index_top += 1
#     top_textline = text_region.text_lines[index_top]
#     # does it start with a capital letter
#     starts_upper = float(top_textline.text[0].isupper())
#     # shift bottom textline until it contains text
#     index_bottom = -1
#     while not text_region.text_lines[index_bottom].text:
#         index_bottom -= 1
#     bottom_textline = text_region.text_lines[index_bottom]
#     # does it end on an eos punctuation mark
#     ends_eos_punctuation = float(bottom_textline.text[-1] in ('.', '!', '?'))
#     # return 2-dimensional feature
#     return [starts_upper, ends_eos_punctuation]


def get_text_regions_wv_sim(text_regions, feature_extractor):
    """
    Generates a feature dictionary with entries for every possible pair of text regions in `text_regions`. The entries
    include text block similarity scores based on language-specific pretrained word vectors, where the full text
    embeddings of each text region are compared via a cosine-like similarity.

    :param text_regions: list of TextRegion objects
    :param feature_extractor: TextblockSimilarity object
    :return: feature dictionary
    """
    # build {tb : text} dict
    tb_dict = dict()
    for text_region in text_regions:
        text = "\n".join([text_line.text for text_line in text_region.text_lines])
        tb_dict[text_region.id] = text
    # run feature extractor
    feature_extractor.set_tb_dict(tb_dict)
    feature_extractor.run()
    return feature_extractor.feature_dict


def get_textline_stroke_widths_heights_dist_trafo(page_path, text_lines, img_path=None):
    """
    Generates two feature dictionaries with entries for each text line given in `text_lines`. The first
    feature contains an approximate stroke width of the text line. The second feature contains an approximate
    text height of the text line.

    A Distance Transform is used on the image file corresponding to the pageXML file given in `page_path`.
    In this transformed image, connected components over the text line bounding boxes are analysed to compute the
    features.

    The stroke width of a text line is set as the median value over the maximum distance values of all contained
    connected components.

    The text height of a text line is set as the maximum height over all ccontained onnected components.

    Prior to this computation, connected components with unreasonable size or aspect ratio get discarded.

    :param page_path: path to pageXML file
    :param text_lines: list of TextLine objects in the pageXML file
    :param img_path: (optional) path to image file to compute the Distance Transform
    :return: (dict1, dict2) where `dict2` contains stroke width features and `dict2` contains text height features
    """
    if img_path is None:
        img_path = get_img_from_page_path(page_path)
    if not img_path:
        raise ValueError(f"Could not find corresponding image file to pagexml '{page_path}'")
    # run SWT
    SWT = StrokeWidthDistanceTransform(dark_on_bright=True)
    swt_img = SWT.distance_transform(img_path)
    # compute stroke widths and text heights on text line level
    textline_stroke_widths = dict()
    textline_heights = dict()
    for text_line in text_lines:
        # build surrounding polygons over text lines
        points_text_line = np.asarray(text_line.surr_p.points_list, dtype=np.int32)
        min_x, max_x, min_y, max_y = get_bounding_box(points_text_line)
        # get swt for text line
        text_line_swt = swt_img[min_y:max_y + 1, min_x:max_x + 1]
        # get connected components in text line
        text_line_ccs = SWT.connected_components_cv(text_line_swt)
        # remove CCs with unreasonable size or aspect ratio
        text_line_ccs = SWT.clean_connected_components(text_line_ccs)
        # go over connected components to estimate stroke width and text height of the text line
        swt_cc_values = []
        text_line_height = 0
        for cc in text_line_ccs:
            # component is a 4-tuple (x, y, width, height)
            # take max value in distance_transform as stroke_width for current CC (can be 0)
            swt_cc_values.append(np.max(text_line_swt[cc[1]: cc[1] + cc[3], cc[0]: cc[0] + cc[2]]))
            # new text height
            if cc[3] > text_line_height:
                text_line_height = cc[3]
        textline_stroke_widths[text_line.id] = np.median(swt_cc_values) if swt_cc_values else 0.0
        textline_heights[text_line.id] = text_line_height
    return textline_stroke_widths, textline_heights


def get_text_region_stroke_width_feature(text_region, textline_stroke_widths, norm=1.0):
    """
    Generates 1d stroke width feature for `text_region`.

    Takes a feature dictionary `textline_stroke_widths`, extracts the text line features corresponding to the given
    text region and then computes the maximum value over those features with an optional normalization factor.

    :param text_region: TextRegion object
    :param textline_stroke_widths: dictionary containing stroke width features for text lines
    :param norm: (optional) normalization factor for resulting feature
    :return: 1d stroke width feature
    """
    # 0-feature for empty text regions
    if all([not line.text for line in text_region.text_lines]):
        return [0.0]
    # maximum stroke width over text lines of text region
    # we prefer the maximum, so headings that are clustered in a block with other text dont get averaged out
    else:
        text_region_stroke_widths = [textline_stroke_widths[line.id] for line in text_region.text_lines if line.text]
        text_region_stroke_width = np.max(text_region_stroke_widths) / norm
        return [text_region_stroke_width]


def get_text_region_text_height_feature(text_region, textline_heights, norm=1.0):
    """
    Generates 1d text height feature for `text_region`.

    Takes a feature dictionary `textline_heights`, extracts the text line features corresponding to the given
    text region and then computes the maximum value over those features with an optional normalization factor.

    :param text_region: TextRegion object
    :param textline_heights: dictionary containing text height features for text lines
    :param norm: (optional) normalization factor for resulting feature
    :return: 1d text height feature
    """
    # 0-feature for empty text regions
    if all([not line.text for line in text_region.text_lines]):
        return [0.0]
    # maximum text height over text lines of text region
    # we prefer the maximum, so headings that are clustered in a block with other text dont get averaged out
    else:
        text_region_line_heights = [textline_heights[line.id] for line in text_region.text_lines if line.text]
        text_region_text_height = np.max(text_region_line_heights) / norm
        return [text_region_text_height]


def get_text_region_heading_feature(text_region):
    """
    Generates 1d (binary) heading feature for `text_region`.

    Checks whether the region type is a 'heading' and computes a corresponding binary feature.

    :param text_region: TextRegion object
    :return: 1d (binary) heading feature
    """
    contains_heading = True if text_region.region_type.lower() == 'heading' else False
    return [float(contains_heading)]


def get_edge_separator_feature_line(text_region_a, text_region_b, separator_regions):
    """
    Generates 2d (binary) separator feature for a pair text regions.

    Checks if `text_region_a` and `text_region_b` are separated by SeparatorRegions given in `separator_regions`.
    It differentiates between horizontal and vertical separators and computes a separate feature for both.
    It is based on edge intersections with separator regions.

    :param text_region_a: TextRegion object
    :param text_region_b: TextRegion object
    :param separator_regions: list of SeparatorRegion objects
    :return: 2d (binary) separator feature (horizontal, vertical)
    """
    # surrounding polygons of text regions
    points_a = np.asarray(text_region_a.points.points_list, dtype=np.int32)
    points_b = np.asarray(text_region_b.points.points_list, dtype=np.int32)
    # bounding boxes of text regions
    min_x_a, max_x_a, min_y_a, max_y_a = get_bounding_box(points_a)
    min_x_b, max_x_b, min_y_b, max_y_b = get_bounding_box(points_b)
    # center of text regions
    center_x_a = (min_x_a + max_x_a) / 2
    center_y_a = (min_y_a + max_y_a) / 2
    center_x_b = (min_x_b + max_x_b) / 2
    center_y_b = (min_y_b + max_y_b) / 2
    # visual line connecting both text regions
    tr_segment = LineString([(center_x_a, center_y_a), (center_x_b, center_y_b)])
    # go over seperator regions and check for intersections
    horizontally_separated = False
    vertically_separated = False
    for separator_region in separator_regions:
        # surrounding polygon of separator region
        points_s = np.asarray(separator_region.points.points_list, dtype=np.int32)
        # bounding box of separator region
        min_x_s, max_x_s, min_y_s, max_y_s = get_bounding_box(points_s)
        # height-width-ratio of bounding box
        width = max(max_x_s - min_x_s, 1)
        height = max(max_y_s - min_y_s, 1)
        ratio = float(height) / float(width)
        # corner points of bounding box
        s1 = (min_x_s, min_y_s)
        s2 = (max_x_s, min_y_s)
        s3 = (min_x_s, max_y_s)
        s4 = (max_x_s, max_y_s)
        # check for intersections/containment between tr_segment and bounding box as prior test
        if line_poly_intersection(tr_segment, [s1, s2, s3, s4]) or \
                line_in_bounding_box(tr_segment, min_x_s, max_x_s, min_y_s, max_y_s):
            # check for intersections between text_region_line and surrounding polygon
            if line_poly_intersection(tr_segment, separator_region.points.points_list):
                sep_orientation = separator_region.get_orientation()
                if sep_orientation == 'horizontal':
                    horizontally_separated = True
                elif separator_region == 'vertical':
                    vertically_separated = True
                else:
                    # ratio check
                    logger.debug(f"No custom orientation tag found for separator region. Defaulting to ratio check.")
                    if ratio < 5:
                        horizontally_separated = True
                    else:
                        vertically_separated = True
                if horizontally_separated and vertically_separated:
                    break
    separator_feature = [float(horizontally_separated), float(vertically_separated)]
    logger.debug(f"{text_region_a.id} - {text_region_b.id}: separators {separator_feature}")
    return separator_feature


def get_bounding_box(points):
    """Returns the bounding box over a set of points."""
    min_x, max_x = np.min(points[:, 0]), np.max(points[:, 0])
    min_y, max_y = np.min(points[:, 1]), np.max(points[:, 1])
    return min_x, max_x, min_y, max_y


def line_poly_intersection(line, polygon):
    """Checks if LineString `line` intersects `polygon` (list of 2d points)."""
    # Optionally close polygon
    if polygon[0] != polygon[-1]:
        polygon.append(polygon[0])
    # Go over polygon segments and check for intersection with line
    for i in range(len(polygon) - 1):
        p1 = polygon[i]
        p2 = polygon[i + 1]
        segment = LineString([p1, p2])
        if line.intersects(segment):
            return True
    return False


def line_in_bounding_box(line, min_x, max_x, min_y, max_y):
    """Checks if LineString `line` is contained in bounding box given by `min_x`, `max_x`, `min_y`, `max_y`."""
    x1, y1, x2, y2 = line.bounds
    if x1 > min_x and x2 < max_x and y1 > min_y and y2 < max_y:
        return True
    return False


def get_edge_separator_feature_bb(text_region_a, text_region_b, separator_regions):
    """
    Generates 2d (binary) separator feature for a pair text regions.

    Checks if `text_region_a` and `text_region_b` are separated by SeparatorRegions given in `separator_regions`.
    It differentiates between horizontal and vertical separators and computes a separate feature for both.
    It is based on rules regarding the bounding boxes of the regions.

    :param text_region_a: TextRegion object
    :param text_region_b: TextRegion object
    :param separator_regions: list of SeparatorRegion objects
    :return: 2d (binary) separator feature (horizontal, vertical)
    """
    # surrounding polygons of text regions
    points_a = np.asarray(text_region_a.points.points_list, dtype=np.int32)
    points_b = np.asarray(text_region_b.points.points_list, dtype=np.int32)
    # bounding boxes of text regions
    bb_a = get_bounding_box(points_a)
    bb_b = get_bounding_box(points_b)
    # go over seperator regions and check for rules
    horizontally_separated = False
    vertically_separated = False
    for separator_region in separator_regions:
        # surrounding polygon of separator region
        points_sep = np.asarray(separator_region.points.points_list, dtype=np.int32)
        # bounding box of separator region
        bb_sep = get_bounding_box(points_sep)
        # separator orientation
        orientation = separator_region.get_orientation()
        if orientation is None:
            # ratio check of bounding box
            width = max(bb_sep[1] - bb_sep[0], 1)
            height = max(bb_sep[3] - bb_sep[2], 1)
            ratio = float(height) / float(width)
            orientation = "horizontal" if ratio < 5 else "vertical"
        # rule checks
        if orientation == "vertical":
            if is_vertically_separated(*bb_a, *bb_b, *bb_sep):
                vertically_separated = True
        else:
            if is_horizontally_separated(*bb_a, *bb_b, *bb_sep):
                horizontally_separated = True
        if horizontally_separated and vertically_separated:
            break
    separator_feature = [float(horizontally_separated), float(vertically_separated)]
    logger.debug(f"{text_region_a.id} - {text_region_b.id}: separators {separator_feature}")
    return separator_feature


def is_vertically_separated(min_x_a, max_x_a, min_y_a, max_y_a,
                            min_x_b, max_x_b, min_y_b, max_y_b,
                            min_x_sep, max_x_sep, min_y_sep, max_y_sep):
    """Rule-based vertical separation criterion based on bounding boxes"""
    mean_x_sep = (min_x_sep + max_x_sep) / 2
    # not horizontally aligned
    if not ((max_x_a <= mean_x_sep <= min_x_b) or  # A - S - B
            (max_x_b <= mean_x_sep <= min_x_a)):  # B - S - A
        return False
    # not atleast one vertically aligned
    if not ((max_y_a >= min_y_sep and min_y_a <= max_y_sep) or  # A | S
            (max_y_b >= min_y_sep and min_y_b <= max_y_sep)):  # S | B
        return False
    return True


def is_horizontally_separated(min_x_a, max_x_a, min_y_a, max_y_a,
                              min_x_b, max_x_b, min_y_b, max_y_b,
                              min_x_sep, max_x_sep, min_y_sep, max_y_sep):
    """Rule-based horizontal separation criterion based on bounding boxes"""
    # not vertically aligned
    if not ((min_y_a <= min_y_sep and max_y_sep <= max_y_b) or  # A over S over B
            (min_y_b <= min_y_sep and max_y_sep <= max_y_a)):  # B over S over A
        return False
    # vertically aligned, but
    # both A & B outside of S on the same side
    if ((max_x_a <= min_x_sep and max_x_b <= min_x_sep) or  # both outside to the left
            (min_x_a >= max_x_sep and min_x_b >= max_x_sep)):  # both outside to the right
        return False
    return True


def get_separator_aligned_regions(separator_regions, text_regions):
    aligned = dict()
    for separator_region in separator_regions:
        # separator orientation
        orientation = separator_region.get_orientation()
        if orientation == 'vertical':
            continue
        # surrounding polygon of separator region
        points_s = np.asarray(separator_region.points.points_list, dtype=np.int32)
        # bounding box of separator region
        min_x_s, max_x_s, min_y_s, max_y_s = get_bounding_box(points_s)
        # ratio check of bounding box
        if orientation is None:
            width = max(max_x_s - min_x_s, 1)
            height = max(max_y_s - min_y_s, 1)
            ratio = float(height) / float(width)
            # orientation = "horizontal" if ratio < 5 else "vertical"
            if ratio >= 5:
                continue
        # initialize dict
        aligned[separator_region.id] = list()
        # go over text regions and check for alignment
        for text_region in text_regions:
            # surrounding polygon of text region
            points_a = np.asarray(text_region.points.points_list, dtype=np.int32)
            # bounding box of text region
            min_x_a, max_x_a, min_y_a, max_y_a = get_bounding_box(points_a)
            # horizontally aligned
            if max_x_a >= min_x_s and min_x_a <= max_x_s:
                aligned[separator_region.id].append(text_region.id)
    return aligned


def is_aligned_horizontally_separated(text_region_a, text_region_b, separator_regions):
    """Function that determines whether two text regions are horizontally separated by a horizontal separator region,
        under the condition that they are vertically aligned"""
    # surrounding polygons of text regions
    points_a = np.asarray(text_region_a.points.points_list, dtype=np.int32)
    points_b = np.asarray(text_region_b.points.points_list, dtype=np.int32)
    # bounding boxes of text regions
    min_x_a, max_x_a, min_y_a, max_y_a = get_bounding_box(points_a)
    min_x_b, max_x_b, min_y_b, max_y_b = get_bounding_box(points_b)
    # go over seperator regions and check for rules
    for separator_region in separator_regions:
        # surrounding polygon of separator region
        points_s = np.asarray(separator_region.points.points_list, dtype=np.int32)
        # bounding box of separator region
        min_x_s, max_x_s, min_y_s, max_y_s = get_bounding_box(points_s)
        # separator orientation
        orientation = separator_region.get_orientation()
        if orientation is None:
            # ratio check of bounding box
            width = max(max_x_s - min_x_s, 1)
            height = max(max_y_s - min_y_s, 1)
            ratio = float(height) / float(width)
            orientation = "horizontal" if ratio < 5 else "vertical"
        # we only care about horizontal separators
        if orientation == 'vertical':
            continue
        # rule check
        # not vertically aligned
        if not ((min_y_a <= min_y_s and max_y_s <= max_y_b) or  # A over S over B
                (min_y_b <= min_y_s and max_y_s <= max_y_a)):  # B over S over A
            continue
        # not horizontally aligned
        if not ((max_x_a >= min_x_s and max_x_b >= min_x_s) and  # max offset to the left
                (min_x_a <= max_x_s and min_x_b <= max_x_s)):  # max offset to the right
            continue
        # is horizontally separated
        return True


def is_aligned_heading_separated(text_region_a, text_region_b):
    # headings
    heading_a = text_region_a.region_type.lower() == 'heading'
    heading_b = text_region_b.region_type.lower() == 'heading'
    # both headings present
    if heading_a and heading_b:
        return False
    # no heading present
    if not (heading_a or heading_b):
        return False
    # surrounding polygons of text regions
    points_a = np.asarray(text_region_a.points.points_list, dtype=np.int32)
    points_b = np.asarray(text_region_b.points.points_list, dtype=np.int32)
    # bounding boxes of text regions
    min_x_a, max_x_a, min_y_a, max_y_a = get_bounding_box(points_a)
    min_x_b, max_x_b, min_y_b, max_y_b = get_bounding_box(points_b)
    # one heading present
    # not horizontally aligned
    if not (min_x_a <= max_x_b and min_x_b <= max_x_a):
        return False
    # one heading present
    if heading_a:
        # heading not vertically lower
        if not (min_y_a >= max_y_b):
            return False
    if heading_b:
        # heading not vertically lower
        if not (min_y_b >= max_y_a):
            return False
    # is heading separated
    return True


def get_node_visual_regions(text_region):
    """Generates visual region for `text_region` as its bounding box."""
    # surrounding polygon of text region
    points = text_region.points.points_list
    # bounding box
    bb = bounding_box(points)
    return bb


def get_edge_visual_regions(text_region_a, text_region_b):
    """Generates visual region regarding a pair of text regions as their convex hull."""
    # surrounding polygons of text regions
    points_a = text_region_a.points.points_list
    points_b = text_region_b.points.points_list
    # convex hull over both regions
    hull = convex_hull(points_a + points_b)
    return hull


def fully_connected_edges(num_nodes):
    """
    Generates a fully-connected edge set (excluding self-loops) for a graph with `num_nodes` nodes.

    :param num_nodes: number of nodes in the graph
    :return: 2d numpy-array representing the edge set
    """
    node_indices = np.arange(num_nodes, dtype=np.int32)
    node_indices = np.tile(node_indices, [num_nodes, 1])
    node_indices_t = np.transpose(node_indices)
    # fully-connected
    interacting_nodes = np.stack([node_indices_t, node_indices], axis=2).reshape([-1, 2])
    # remove self-loops
    del_indices = np.arange(num_nodes) * (num_nodes + 1)
    interacting_nodes = np.delete(interacting_nodes, del_indices, axis=0)
    return interacting_nodes


def delaunay_edges(num_nodes, node_positions):
    """
    Generates a Delaunay triangulation as the edge set for a graph with `num_nodes` nodes.

    :param num_nodes: number of nodes in the graph
    :param node_positions: 2d array containing the geometric positions of the nodes
    :return: 2d numpy-array representing the edge set
    """
    # round to nearest 50px for a more homogenous layout
    node_positions_smooth = round_base(node_positions, base=50)
    # interacting nodes are neighbours in the delaunay triangulation
    try:
        delaunay = Delaunay(node_positions_smooth)
    except QhullError:
        logger.warning("Delaunay input has the same x-coords. Defaulting to unsmoothed data.")
        delaunay = Delaunay(node_positions)
    indice_pointer, indices = delaunay.vertex_neighbor_vertices
    interacting_nodes = []
    for v in range(num_nodes):
        neighbors = indices[indice_pointer[v]:indice_pointer[v + 1]]
        interaction = np.stack(np.broadcast_arrays(v, neighbors), axis=1)
        interacting_nodes.append(interaction)
    interacting_nodes = np.concatenate(interacting_nodes, axis=0)
    return interacting_nodes


def discard_text_regions_and_lines(text_regions, text_lines=None):
    """Discards text regions (and their corresponding text lines) if they either a) do not contain any
    text or b) have too small of a bounding box."""
    # discard regions
    discard = 0
    text_lines_to_remove = []
    for tr in text_regions.copy():
        # ... without text
        if not tr.text_lines or all([text_line.text == "" for text_line in tr.text_lines]):
            text_regions.remove(tr)
            logger.debug(f"Discarding TextRegion {tr.id} (no text)")
            discard += 1
            continue
        # ... too small
        bounding_box = tr.points.to_polygon().get_bounding_box()
        if bounding_box.width < 10 or bounding_box.height < 10:
            text_regions.remove(tr)
            logger.debug(f"Discarding TextRegion {tr.id} (bounding box too small, height={bounding_box.height}, "
                         f"width={bounding_box.width})")
            if text_lines:
                for text_line in tr.text_lines:
                    text_lines_to_remove.append(text_line.id)
            discard += 1
    # discard corresponding text lines
    if text_lines_to_remove:
        text_lines = [line for line in text_lines if line.id not in text_lines_to_remove]
    if discard > 0:
        logger.warning(f"Discarded {discard} degenerate text_region(s). Either no text or region too small.")
    return text_regions, text_lines


def get_data_from_pagexml(path_to_pagexml):
    """ Extracts information contained by in a pageXML file given by `path_to_pagexml`.

    :param path_to_pagexml: file path of the pageXML
    :return: dict of regions, list of text lines, list of baselines, list of article ids, image resolution
    """
    page_file = Page(path_to_pagexml)
    dict_of_regions = page_file.get_regions()
    list_of_txt_lines = page_file.get_textlines()
    _, region_article_dict = page_file.get_article_region_dicts()
    page_resolution = page_file.get_image_resolution()
    return dict_of_regions, list_of_txt_lines, region_article_dict, page_resolution


def build_input_and_target(page_path,
                           interaction='delaunay',
                           separators="bb",
                           visual_regions=False,
                           external_data=None,
                           sim_feat_extractor=None):
    """
    Computation of the input and target values to solve the article separation problem with a graph neural
    network on text region (baseline clusters) level.

    Generates the underlying graph structure (edge set), the node and edge features as well as the target
    ground truth relations.

    :param page_path: path to pageXML file
    :param interaction: method for edge set generation ('delaunay' or 'fully')
    :param separators: method for edge separator features ('bb' or 'line')
    :param visual_regions: (bool) optionally build visual regions for nodes and edges (default False)
    :param external_data: (optional) list of additonal feature dictionaries from external json sources
    :param sim_feat_extractor: (optional) TextblockSimilarity feature extractor
    :return: 'num_nodes', 'interacting_nodes', 'num_interacting_nodes' ,'node_features', 'edge_features',
        'visual_region_nodes', 'num_points_visual_region_nodes', 'visual_region_edges',
        'num_points_visual_region_edges', 'gt_relations', 'gt_num_relations'
    """
    assert interaction in ('fully', 'delaunay'), \
        f"Interaction setup {interaction} is not supported. Choose from ('fully', 'delaunay') instead."

    assert separators in ('line', 'bb'), \
        f"Separator feature setup {separators} is not supported. Choose from ('line', 'bb') instead."

    # load page data
    regions, text_lines, article_dict, resolution = get_data_from_pagexml(page_path)
    norm_x, norm_y = float(resolution[0]), float(resolution[1])
    try:
        text_regions = regions['TextRegion']
    except KeyError:
        logger.warning(f'No TextRegions found in {page_path}. Returning None.')
        return None, None, None, None, None, None, None, None, None, None, None

    # discard TextRegions and corresponding TextLines if necessary
    text_regions, text_lines = discard_text_regions_and_lines(text_regions, text_lines)

    # number of nodes
    num_nodes = len(text_regions)
    if num_nodes <= 1:
        logger.warning(f'Less than two nodes found in {page_path}. Returning None.')
        return None, None, None, None, None, None, None, None, None, None, None

    # pre-compute stroke width and height over textlines (and their maximum value for normalization)
    textline_stroke_widths, textline_heights = get_textline_stroke_widths_heights_dist_trafo(page_path, text_lines)
    sw_max = np.max(list(textline_stroke_widths.values()))
    th_max = np.max(list(textline_heights.values()))

    # node features
    node_features = []
    # compute region features
    for text_region in text_regions:
        node_feature = []
        # region geometric feature (4-dim)
        node_feature.extend(get_text_region_geometric_features(text_region, norm_x, norm_y))
        # top/bottom baseline geometric feature (8-dim)
        node_feature.extend(get_text_region_baseline_features(text_region, norm_x, norm_y))
        # # punctuation feature (2-dim)
        # node_feature.extend(get_text_region_punctuation_feature(text_region))
        # stroke width feature (1-dim)
        node_feature.extend(get_text_region_stroke_width_feature(text_region, textline_stroke_widths, norm=sw_max))
        # text height feature (1-dim)
        node_feature.extend(get_text_region_text_height_feature(text_region, textline_heights, norm=th_max))
        # heading feature (1-dim)
        node_feature.extend(get_text_region_heading_feature(text_region))
        # external features
        if external_data:
            for ext in external_data:
                try:
                    ext_page = ext[os.path.basename(page_path)]
                except KeyError:
                    logger.warning(f'Could not find key {os.path.basename(page_path)} in external data json.')
                    continue
                if 'node_features' in ext_page:
                    try:
                        node_feature.extend(ext_page['node_features'][text_region.id])
                    except KeyError:
                        logger.debug(f"Could not find entry node_features->{text_region.id} in external json. "
                                     f"Defaulting.")
                        try:
                            node_feature.extend([ext_page['node_features']['default']])
                        except KeyError:
                            logger.debug(f"Could not find entry node_features->default in external json. Using 0.0.")
                            node_feature.extend([0.0])
        # final node feature vector
        node_features.append(node_feature)

    # interacting nodes (edge set)
    if interaction == 'fully' or num_nodes < 4:
        interacting_nodes = fully_connected_edges(num_nodes)
    else:  # delaunay
        node_centers = np.array(node_features, dtype=np.float32)[:, 2:4] * [norm_x, norm_y]
        interacting_nodes = delaunay_edges(num_nodes, node_centers)

    # number of interacting nodes
    num_interacting_nodes = interacting_nodes.shape[0]

    # pre-compute text block similarities with word vectors
    tb_sim_dict = get_text_regions_wv_sim(text_regions, sim_feat_extractor) if sim_feat_extractor is not None else None

    # regions for separator features
    separator_regions = regions['SeparatorRegion'] if 'SeparatorRegion' in regions else None

    # edge features for each pair of interacting nodes
    edge_features = []
    for i in range(num_interacting_nodes):
        edge_feature = []
        node_a, node_b = interacting_nodes[i, 0], interacting_nodes[i, 1]
        text_region_a, text_region_b = text_regions[node_a], text_regions[node_b]
        # separator feature (2-dim)
        if separator_regions:
            if separators == 'line':
                edge_feature.extend(get_edge_separator_feature_line(text_region_a, text_region_b, separator_regions))
            else:  # separators 'bb' default
                edge_feature.extend(get_edge_separator_feature_bb(text_region_a, text_region_b, separator_regions))
        else:
            edge_feature.extend([0.0, 0.0])
        # text block similarity features based on word vectors
        if tb_sim_dict:
            try:
                edge_feature.extend(tb_sim_dict['edge_features'][text_region_a.id][text_region_b.id])
            except KeyError:
                logger.debug(f"Could not find entry edge_features->{text_region_a.id}->{text_region_b.id} in "
                             f"text block similarity dict. Defaulting.")
                try:
                    edge_feature.extend(tb_sim_dict['edge_features']['default'])
                except KeyError:
                    logger.debug(f"Could not find entry edge_features->default in "
                                 f"text block similarity dict. Using 0.5.")
                    edge_feature.extend([0.5])
        # external features
        if external_data:
            for ext in external_data:
                try:
                    ext_page = ext[os.path.basename(page_path)]
                except KeyError:
                    logger.warning(f'Could not find key {os.path.basename(page_path)} in external data json. Skipping.')
                    continue
                if 'edge_features' in ext_page:
                    try:
                        edge_feature.extend(ext_page['edge_features'][text_region_a.id][text_region_b.id])
                    except (KeyError, TypeError):
                        logger.debug(f"Could not find entry edge_features->{text_region_a.id}->{text_region_b.id} in "
                                     f"external json. Defaulting.")
                        try:
                            edge_feature.extend(ext_page['edge_features']['default'])
                        except KeyError:
                            logger.debug(f"Could not find entry edge_features->default in external json. Using 0.5.")
                            edge_feature.extend([0.5])
        # final edge feature vector
        edge_features.append(edge_feature)

    # visual regions for nodes (for GNN visual features)
    visual_regions_nodes = []
    num_points_visual_regions_nodes = []
    if visual_regions:
        for text_region in text_regions:
            visual_regions_node = get_node_visual_regions(text_region)
            visual_regions_nodes.append(visual_regions_node)
            num_points_visual_regions_nodes.append(len(visual_regions_node))

    # visual regions for edges (for GNN visual features)
    visual_regions_edges = []
    num_points_visual_regions_edges = []
    if visual_regions:
        for i in range(num_interacting_nodes):
            node_a, node_b = interacting_nodes[i, 0], interacting_nodes[i, 1]
            text_region_a, text_region_b = text_regions[node_a], text_regions[node_b]
            visual_regions_edge = get_edge_visual_regions(text_region_a, text_region_b)
            visual_regions_edges.append(visual_regions_edge)
            num_points_visual_regions_edges.append(len(visual_regions_edge))

        # build padded array
        # make faster?
        # https://stackoverflow.com/questions/53071212/stacking-numpy-arrays-with-padding
        # https://stackoverflow.com/questions/53051560/stacking-numpy-arrays-of-different-length-using-padding/53052599?noredirect=1#comment93005810_53052599
        visual_regions_edges_array = np.zeros((num_interacting_nodes, np.max(num_points_visual_regions_edges), 2))
        for i in range(num_interacting_nodes):
            visual_region = visual_regions_edges[i]
            visual_regions_edges_array[i, :len(visual_region), :] = visual_region

    # ground-truth relations
    gt_relations = []
    num_tr_ambiguous = 0
    tr_gt_article_ids = []
    for text_region in text_regions:
        # get article_ids for this region
        tr_article_ids = article_dict[text_region.id]
        if isinstance(tr_article_ids, list) and len(tr_article_ids) > 1:
            num_tr_ambiguous += 1
            assign_id = tr_article_ids[0]
            tr_gt_article_ids.append(assign_id)
            logger.warning(f"TextRegion {text_region.id}: Found mulitple article_ids {tr_article_ids}, "
                           f"assigning article_id '{assign_id}'.")
        else:
            tr_gt_article_ids.append(tr_article_ids)
    logger.debug(f"{num_tr_ambiguous}/{len(text_regions)} had ambiguous article relations.")
    # build gt ("1" means 'belong_to_same_article')
    for i, i_id in enumerate(tr_gt_article_ids):
        for j, j_id in enumerate(tr_gt_article_ids):
            if i_id == j_id:
                gt_relations.append([1, i, j])

    # number of ground-truth relations
    gt_num_relations = len(gt_relations)

    return np.array(num_nodes, dtype=np.int32), \
           interacting_nodes.astype(np.int32), \
           np.array(num_interacting_nodes, dtype=np.int32), \
           np.array(node_features, dtype=np.float32), \
           np.array(edge_features, dtype=np.float32) if edge_features else None, \
           np.transpose(np.array(visual_regions_nodes, dtype=np.float32), axes=(0, 2, 1)) if visual_regions else None, \
           np.array(num_points_visual_regions_nodes, dtype=np.int32) if visual_regions else None, \
           np.transpose(visual_regions_edges_array, axes=(0, 2, 1)) if visual_regions else None, \
           np.array(num_points_visual_regions_edges, dtype=np.int32) if visual_regions else None, \
           np.array(gt_relations, dtype=np.int32), \
           np.array(gt_num_relations, dtype=np.int32)


def generate_feature_jsons(page_paths,
                           out_path=None,
                           interaction="delaunay",
                           visual_regions=True,
                           json_list=None,
                           tb_similarity_setup=(None, None),
                           separators="line"):
    """
    Generates the input json files for a Graph Neural Network regarding the article separation task.

    For each pageXML file given in `page_paths` a corresponding json file will be generated, which contains the
    graph structure, node and edge features as well as the target relations.

    :param page_paths: list of pageXML file paths
    :param out_path: (optional) folder path to save the output to (defaults to a new 'json' folder besides the
        'page' folder where the pageXMl files are from)
    :param interaction: method for edge set generation ('delaunay' or 'fully')
    :param visual_regions: (bool) optionally build visual regions for nodes and edges (default False)
    :param json_list: (optional) list of additonal feature dictionaries from external json sources
    :param tb_similarity_setup: (optional) tuple ('language', 'wv_path'), where `language` is a string describing the
        underlying language of the word vector model given by `wv_path`
    :return: None
    """
    # Get external json data
    json_data = []
    if json_list:
        json_timer = time.time()
        for json_path in json_list:
            with open(json_path, "r") as json_file:
                json_data.append(json.load(json_file))
        logger.info(f"Time (loading external jsons): {time.time() - json_timer:.2f} seconds")

    # Setup textblock similarity feature extractor
    sim_feat_extractor = None
    if tb_similarity_setup[0] and tb_similarity_setup[1]:
        sim_feat_extractor = TextblockSimilarity(language=tb_similarity_setup[0], wv_path=tb_similarity_setup[1])

    # Get data from pagexml and write to json
    create_default_dir = False if out_path else True
    skipped_pages = []
    start_timer = time.time()
    for page_path in page_paths:
        logger.info(f"Processing... {page_path}")
        # build input & target
        num_nodes, interacting_nodes, num_interacting_nodes, node_features, edge_features, \
        visual_regions_nodes, num_points_visual_regions_nodes, \
        visual_regions_edges, num_points_visual_regions_edges, \
        gt_relations, gt_num_relations = \
            build_input_and_target(page_path=page_path,
                                   interaction=interaction,
                                   visual_regions=visual_regions,
                                   external_data=json_data,
                                   sim_feat_extractor=sim_feat_extractor,
                                   separators=separators)

        # build and write output
        if num_nodes is not None:
            out_dict = dict()
            out_dict["num_nodes"] = num_nodes.tolist()
            out_dict['interacting_nodes'] = interacting_nodes.tolist()
            out_dict['num_interacting_nodes'] = num_interacting_nodes.tolist()
            out_dict['node_features'] = node_features.tolist()
            out_dict['edge_features'] = edge_features.tolist()
            if visual_regions_nodes is not None and num_points_visual_regions_nodes is not None:
                out_dict['visual_regions_nodes'] = visual_regions_nodes.tolist()
                out_dict['num_points_visual_regions_nodes'] = num_points_visual_regions_nodes.tolist()
            if visual_regions_edges is not None and num_points_visual_regions_edges is not None:
                out_dict['visual_regions_edges'] = visual_regions_edges.tolist()
                out_dict['num_points_visual_regions_edges'] = num_points_visual_regions_edges.tolist()
            out_dict['gt_relations'] = gt_relations.tolist()
            out_dict['gt_num_relations'] = gt_num_relations.tolist()

            # Default output is a json folder one level above the pagexml file, indicating features and interaction
            if create_default_dir:
                visual = 'v' if visual_regions else ''
                out_path = re.sub(r'page$',
                                  f'json{node_features.shape[1]}{interaction[0]}{edge_features.shape[1]}{visual}',
                                  os.path.dirname(page_path))
            # Create output directory
            if not os.path.isdir(out_path):
                os.makedirs(out_path)
                logger.info(f"Created output directory {out_path}")

            # Dump jsons
            file_name = os.path.splitext(os.path.basename(page_path))[0] + ".json"
            out = os.path.join(out_path, file_name)
            with open(out, "w") as out_file:
                json.dump(out_dict, out_file)
                logger.info(f"Saved json with graph features '{out}'")
        else:
            skipped_pages.append(page_path)
    logger.info(f"Time (feature generation): {time.time() - start_timer:.2f} seconds")
    logger.info(f"Wrote {len(page_paths) - len(skipped_pages)}/{len(page_paths)} files.")
    logger.info(f"Skipped {len(skipped_pages)} files:")
    for skipped in skipped_pages:
        logger.info(f"'{skipped}'")


if __name__ == '__main__':
    page_path = "/home/johannes/devel/TEMP/koeln112_as_gt_test_relations/AS_GT_Koeln_Relations_validation/page/" \
                "0001_Koelnische_Zeitung._1803-1945_95_96_(21.2.1936)_Seite_12.xml"

    num_nodes, interacting_nodes, num_interacting_nodes, node_features, edge_features, \
    visual_regions_nodes, num_points_visual_regions_nodes, \
    visual_regions_edges, num_points_visual_regions_edges, \
    gt_relations, gt_num_relations = \
        build_input_and_target(page_path=page_path, interaction="delaunay", separators="bb")

    from citlab_article_separation.gnn.io import plot_graph_and_page, create_undirected_graph, build_weighted_relation_graph
    graph = build_weighted_relation_graph(interacting_nodes,
                                          [0.0 for i in range(len(interacting_nodes))],
                                          [{'separated': bool(e)} for e in edge_features[:, :1].flatten()])
    graph = create_undirected_graph(graph, reciprocal=False)

    os.chdir("/home/johannes/devel/TEMP/koeln112_as_gt_test_relations")
    save_dir = "/home/johannes/devel/TEMP/koeln112_as_gt_test_relations"
    plot_graph_and_page(graph, node_features, page_path, save_dir,
                        threshold=0.5, info="", name="myNAME", with_edges=True, with_labels=True)

    #########################

    # page_list = "/home/johannes/devel/projects/tf_rel/lists/onb230/onb_230_gt.lst"
    # bert_path = "/home/johannes/devel/projects/tf_rel/resources/bert/pred_onb_230_for_bert.json"
    # ulr_path = "/home/johannes/devel/projects/tf_rel/data/GT_ULR/conf_ULR/ulr_onb_230_conf.json"
    #
    # # Get external json data
    # json_timer = time.time()
    # with open(bert_path, "r") as bert_file:
    #     bert_data = json.load(bert_file)
    # with open(ulr_path, "r") as ulr_file:
    #     ulr_data = json.load(ulr_file)
    # logger.info(f"Time (loading external jsons): {time.time() - json_timer:.2f} seconds")
    #
    # # Setup textblock similarity feature extractor
    # wv_lang = "german"
    # wv_path = "/home/johannes/devel/projects/tf_rel/resources/newseye_de_300.w2v"
    # sim_feat_extractor = TextblockSimilarity(language=wv_lang, wv_path=wv_path)
    #
    # page_paths = [line.rstrip() for line in open(page_list, "r")]
    # for page_path in page_paths:
    #     regions, text_lines, baselines, article_ids, resolution = get_data_from_pagexml(page_path)
    #     try:
    #         text_regions = regions['TextRegion']
    #     except KeyError:
    #         logger.error(f'No TextRegions found in {page_path}. Skipping.')
    #         continue
    #
    #     # discard TextRegions and corresponding TextLines if necessary
    #     text_regions, text_lines = discard_text_regions_and_lines(text_regions, text_lines)
    #
    #     # number of nodes
    #     num_nodes = len(text_regions)
    #     if num_nodes <= 1:
    #         logger.warning(f'Less than two nodes found in {page_path}. Skipping.')
    #         continue
    #
    #     # pre-compute text block similarities with word vectors
    #     tb_sim_dict = get_text_regions_wv_sim(text_regions, sim_feat_extractor)
    #
    #     logger.info(f"PAGE {page_path}")
    #     for i in range(len(text_regions)):
    #         for j in range(i+1, len(text_regions)):
    #             text_region_a, text_region_b = text_regions[i], text_regions[j]
    #             try:
    #                 sim_wv_ij = tb_sim_dict['edge_features'][text_region_a.id][text_region_b.id][0]
    #                 sim_wv_ji = tb_sim_dict['edge_features'][text_region_b.id][text_region_a.id][0]
    #             except KeyError:
    #                 logger.error(f"Could not find entry edge_features->{text_region_a.id}->{text_region_b.id} in "
    #                              f"text block similarity dict. Defaulting.")
    #                 try:
    #                     sim_wv_ij = tb_sim_dict['edge_features']['default'][0]
    #                     sim_wv_ji = tb_sim_dict['edge_features']['default'][0]
    #                 except KeyError:
    #                     logger.error(f"Could not find entry edge_features->default in "
    #                                  f"text block similarity dict. Using 0.5.")
    #                     sim_wv_ij = 0.5
    #                     sim_wv_ji = 0.5
    #             # bert features
    #             try:
    #                 ext_page = bert_data[os.path.basename(page_path)]
    #             except KeyError:
    #                 logger.warning(
    #                     f'Could not find key {os.path.basename(page_path)} in external data json. Skipping.')
    #                 continue
    #             try:
    #                 sim_bert_ij = ext_page['edge_features'][text_region_a.id][text_region_b.id][0]
    #                 sim_bert_ji = ext_page['edge_features'][text_region_b.id][text_region_a.id][0]
    #             except (KeyError, TypeError):
    #                 logger.error(
    #                     f"Could not find entry edge_features->{text_region_a.id}->{text_region_b.id} in "
    #                     f"external json. Defaulting.")
    #                 try:
    #                     sim_bert_ij = ext_page['edge_features']['default'][0]
    #                     sim_bert_ji = ext_page['edge_features']['default'][0]
    #                 except KeyError:
    #                     logger.error(
    #                         f"Could not find entry edge_features->default in external json. Using 0.5.")
    #                     sim_bert_ij = 0.5
    #                     sim_bert_ji = 0.5
    #             # ulr features
    #             try:
    #                 ext_page = ulr_data[os.path.basename(page_path)]
    #             except KeyError:
    #                 logger.warning(
    #                     f'Could not find key {os.path.basename(page_path)} in external data json. Skipping.')
    #                 continue
    #             try:
    #                 sim_ulr_ij = ext_page['edge_features'][text_region_a.id][text_region_b.id][0]
    #                 sim_ulr_ji = ext_page['edge_features'][text_region_b.id][text_region_a.id][0]
    #             except (KeyError, TypeError):
    #                 logger.error(
    #                     f"Could not find entry edge_features->{text_region_a.id}->{text_region_b.id} in "
    #                     f"external json. Defaulting.")
    #                 try:
    #                     sim_ulr_ij = ext_page['edge_features']['default'][0]
    #                     sim_ulr_ji = ext_page['edge_features']['default'][0]
    #                 except KeyError:
    #                     logger.error(
    #                         f"Could not find entry edge_features->default in external json. Using 0.5.")
    #                     sim_ulr_ij = 0.5
    #                     sim_ulr_ji = 0.5
    #
    #             logger.info(f"---TextBlocks {i}-{j}")
    #             logger.info(f"------BERT ({i}-{j})={sim_bert_ij:.2f}, ({j}-{i})={sim_bert_ji:.2f}")
    #             logger.info(f"------ ULR ({i}-{j})={sim_ulr_ij:.2f}, ({j}-{i})={sim_ulr_ji:.2f}")
    #             logger.info(f"------  WV ({i}-{j})={sim_wv_ij:.2f}, ({j}-{i})={sim_wv_ji:.2f}")

    #########################

    # page_path = "/home/johannes/pr-00006.xml"
    # page = Page(page_path)
    # text_regions = page.get_text_regions()
    # print(text_regions[0].id)
    # # text_regions, _ = discard_text_regions_and_lines(text_regions)
    #
    # # load page data
    # regions, text_lines, baselines, article_ids, resolution = get_data_from_pagexml(page_path)
    # norm_x, norm_y = float(resolution[0]), float(resolution[1])
    # try:
    #     text_regions = regions['TextRegion']
    # except KeyError:
    #     logger.warning(f'No TextRegions found in {page_path}. Returning None.')
    # print(text_regions[0].id)

    #########################

    # list_paths = list()
    # list_paths.append("/home/johannes/devel/projects/tf_rel/lists/bnf183/bnf_183_gt.lst")
    # list_paths.append("/home/johannes/devel/projects/tf_rel/lists/nlf200/nlf_200_gt.lst")
    # list_paths.append("/home/johannes/devel/projects/tf_rel/lists/onb230/onb_230_gt.lst")
    #
    # max_articles = dict()
    # for list_path in list_paths:
    #     name = os.path.basename(list_path)
    #     page_paths = [line.rstrip() for line in open(list_path, "r")]
    #     max_num_articles = 0
    #     for page_path in page_paths:
    #         page = Page(page_path)
    #         num_articles = len(list(page.get_article_dict().keys()))
    #         print(f"{num_articles} articles in '{page_path}'")
    #         if num_articles > max_num_articles:
    #             max_num_articles = num_articles
    #     max_articles[name] = max_num_articles
    #
    # for key in max_articles:
    #     print(f"{key}: max {max_articles[key]} articles.")
