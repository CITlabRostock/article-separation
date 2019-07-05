from citlab_python_util.geometry.util import ortho_connect, smooth_surrounding_polygon, polygon_clip, convex_hull, \
    bounding_box
from citlab_python_util.parser.xml.page.page import Page
from citlab_python_util.parser.xml.page.page_objects import Points

from citlab_article_separation.article_rectangle import ArticleRectangle


def get_article_surrounding_polygons(ar_dict):
    """
    Create surrounding polygons over sets of rectangles, belonging to different article_ids.

    :param ar_dict: dict (keys = article_id, values = corresponding rectangles)
    :return: dict (keys = article_id, values = corresponding surrounding polygons)
    """
    asp_dict = {}
    for id in ar_dict:
        sp = ortho_connect(ar_dict[id])
        asp_dict[id] = sp
    return asp_dict


def smooth_article_surrounding_polygons(asp_dict, poly_norm_dist=10, orientation_dims=(600, 300, 600, 300), offset=0):
    """
    Create smoothed polygons over "crooked" polygons, belonging to different article_ids.

    1.) The polygon gets normalized, where the resulting vertices are at most `poly_norm_dist` pixels apart.

    2.) For each vertex of the original polygon an orientation is determined:

    2.1) Four rectangles (North, East, South, West) are generated, with the dimensions given by `or_dims`
    (width_vertical, height_vertical, width_horizontal, height_horizontal), i.e. North and South rectangles
    have dimensions width_v x height_v, whereas East and West rectangles have dimensions width_h x height_h.

    2.2) The offset controls how far the cones overlap (e.g. how far the north cone gets translated south)

    2.3) Each rectangle counts the number of contained points from the normalized polygon

    2.4) The top two rectangle counts determine the orientation of the vertex: vertical, horizontal or one
    of the four possible corner types.

    3.) Vertices with a differing orientation to its agreeing neighbours are assumed to be mislabeled and
    get its orientation converted to its neighbours.

    4.) Corner clusters of the same type need to be shrunken down to one corner, with the rest being converted
    to verticals. (TODO or horizontals)

    5.) Clusters between corners (corner-V/H-...-V/H-corner) get smoothed if they contain at least five points,
    by taking the average over the y-coordinates for horizontal edges and the average over the x-coordinates for
    vertical edges.

    :param asp_dict: dict (keys = article_id, values = list of "crooked" polygons)
    :param poly_norm_dist: int, distance between pixels in normalized polygon
    :param orientation_dims: tuple (width_v, height_v, width_h, height_h), the dimensions of the orientation rectangles
    :param offset: int, number of pixel that the orientation cones overlap
    :return: dict (keys = article_id, values = smoothed polygons)
    """
    asp_dict_smoothed = {}
    for id in asp_dict:
        asp_dict_smoothed[id] = []
        for poly in asp_dict[id]:
            sp_smooth = smooth_surrounding_polygon(poly, poly_norm_dist, orientation_dims, offset)
            asp_dict_smoothed[id].append(sp_smooth)
    return asp_dict_smoothed


def convert_blank_article_rects_by_rects(ars_dict, method="bb"):
    assert method == "bb" or method == "ch", "Only supports methods 'bb' (bounding boxes) and 'ch' (convex hulls)"
    # Build up bounding boxes / convex hulls over rectangle vertices
    poly_dict = {}
    for key in ars_dict:
        if key == "blank" or key is None:
            continue
        article_point_set = []
        for ar in ars_dict[key]:
            article_point_set += ar.get_vertices()
        if method == "bb":
            poly_dict[key] = bounding_box(article_point_set)
        elif method == "ch":
            poly_dict[key] = convex_hull(article_point_set)

    out_dict = ars_dict.copy()
    to_remove = []
    # Go over blank rectangles and check for intersections with other articles
    for ar in ars_dict["blank"]:
        intersections = []
        for key in poly_dict:
            if polygon_clip(ar.get_vertices(), poly_dict[key]):
                intersections.append(key)
        # got exactly 1 intersection
        if len(intersections) == 1:
            # Convert rectangle to respective article id
            out_dict[intersections[0]].append(ar)
            to_remove.append(ar)
    # Remove relevant rectangles from blanks
    out_dict["blank"] = [ar for ar in ars_dict["blank"] if ar not in to_remove]
    return out_dict


def convert_blank_article_rects_by_polys(ars_dict, asp_dict, method="bb"):
    assert method == "bb" or method == "ch", "Only supports methods 'bb' (bounding boxes) and 'ch' (convex hulls)"
    # Build up bounding boxes / convex hulls over polygon vertices
    poly_dict = {}
    for key in asp_dict:
        if key == "blank" or key is None:
            continue
        poly_dict[key] = []
        for sp in asp_dict[key]:
            if method == "bb":
                poly_dict[key].append(bounding_box(sp.as_list()))
            elif method == "ch":
                poly_dict[key].append(convex_hull(sp.as_list()))

    out_dict = ars_dict.copy()
    to_remove = []
    # Go over blank rectangles and check for intersections with other articles
    for ar in ars_dict["blank"]:
        intersections = []
        for key in poly_dict:
            for poly in poly_dict[key]:
                if polygon_clip(ar.get_vertices(), poly):
                    intersections.append(key)
        # got exactly 1 intersection
        print("AR: {}".format(ar.get_vertices()))
        print("Intersections: {}".format(intersections))
        if len(set(intersections)) == 1:
            # Convert rectangle to respective article id
            out_dict[intersections[0]].append(ar)
            to_remove.append(ar)
    # Remove relevant rectangles from blanks
    out_dict["blank"] = [ar for ar in ars_dict["blank"] if ar not in to_remove]
    return out_dict


def get_article_rectangles(page, use_max_rect_size=True, max_d=0, max_rect_size_scale=1 / 50, max_d_scale=1 / 20):
    """Given the PageXml file `page` return the corresponding article subregions as a list of ArticleRectangle objects.
     Also returns the width and height of the image (NOT of the PrintSpace).

    :param page: Either the path to the PageXml file or a Page object.
    :type page: Union[str, Page]
    :param use_max_rect_size: whether to use a max rectangle size for the article rectangles or not
    :type use_max_rect_size: bool
    :return: the article subregion list, the height and the width of the image
    """
    if type(page) == str:
        page = Page(page)

    assert type(page) == Page, f"Type must be Page, got {type(page)} instead."
    ps_coords = page.get_print_space_coords()
    ps_poly = Points(ps_coords).to_polygon()
    # Maybe check if the surrounding Rectangle of the polygon has corners given by ps_poly
    ps_rectangle = ps_poly.get_bounding_box()

    # First ArticleRectangle to consider
    ps_rectangle = ArticleRectangle(ps_rectangle.x, ps_rectangle.y, ps_rectangle.width, ps_rectangle.height,
                                    page.get_textlines())

    if use_max_rect_size:
        max_rect_size = int(max_rect_size_scale * ps_rectangle.height)
    else:
        max_rect_size = 0
    if not max_d:
        max_d = int(max_d_scale * ps_rectangle.height)

    ars = ps_rectangle.create_subregions(max_d=max_d, max_rect_size=max_rect_size)

    # ars = ps_rectangle.create_subregions(max_d=int(1 / 20 * ps_rectangle.height))

    img_width, img_height = page.get_image_resolution()

    return ars, img_height, img_width
