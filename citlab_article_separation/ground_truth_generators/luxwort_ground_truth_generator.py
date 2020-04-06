import argparse

from citlab_article_separation.ground_truth_generators.text_block_ground_truth_generator import \
    TextBlockGroundTruthGenerator


class LuxwortGroundTruthGenerator(TextBlockGroundTruthGenerator):
    def __init__(self, path_to_img_lst, fixed_height=0, scaling_factor=1.0, use_bounding_box=False,
                 use_min_area_rect=False):
        super().__init__(path_to_img_lst, fixed_height, scaling_factor, use_bounding_box, use_min_area_rect)
        self.advert_regions = self.get_advert_regions_list()
        self.table_regions = self.get_table_regions_list()

        self.title_headline_regions = self.get_title_regions_list(["headline"])
        self.title_subheadline_regions = self.get_title_regions_list(["subheadline", "motto"])
        self.title_publ_stmt_regions = self.get_title_regions_list(["publishing_stmt"])
        self.title_other_regions = self.get_title_regions_list(["other"])

        self.heading_title_regions = self.get_classic_heading_regions_list([""])
        self.heading_overline_regions = self.get_classic_heading_regions_list(["overline"])
        self.heading_subheadline_regions = self.get_classic_heading_regions_list(["subheadline"])
        self.heading_author_regions = self.get_classic_heading_regions_list(["author"])
        self.heading_other_regions = self.get_classic_heading_regions_list(["other"])

    def create_ground_truth_images(self):
        # Order of gt images is important for the "make_disjoint_all()" call at the end.
        for i in range(len(self.img_path_lst)):
            img_width = self.img_res_lst[i][1]
            img_height = self.img_res_lst[i][0]
            sc_factor = self.scaling_factors[i]

            table_gt_img = self.create_region_gt_img(self.table_regions[i], img_width, img_height, fill=True,
                                                     scaling_factor=sc_factor)
            advert_gt_img = self.create_region_gt_img(self.advert_regions[i], img_width, img_height, fill=True,
                                                      scaling_factor=sc_factor)
            title_headline_gt_img = self.create_region_gt_img(self.title_headline_regions[i], img_width, img_height,
                                                              fill=True, scaling_factor=sc_factor)
            title_subheadline_gt_img = self.create_region_gt_img(self.title_subheadline_regions[i], img_width,
                                                                 img_height, fill=True, scaling_factor=sc_factor)
            title_publ_stmt_gt_img = self.create_region_gt_img(self.title_publ_stmt_regions[i], img_width, img_height,
                                                               fill=True, scaling_factor=sc_factor)
            title_other_gt_img = self.create_region_gt_img(self.title_other_regions[i], img_width, img_height,
                                                           fill=True, scaling_factor=sc_factor)
            heading_title_gt_img = self.create_region_gt_img(self.heading_title_regions[i], img_width, img_height,
                                                             fill=True, scaling_factor=sc_factor)
            heading_overline_gt_img = self.create_region_gt_img(self.heading_overline_regions[i], img_width, img_height,
                                                                fill=True, scaling_factor=sc_factor)
            heading_subheadline_gt_img = self.create_region_gt_img(self.heading_subheadline_regions[i], img_width,
                                                                   img_height, fill=True, scaling_factor=sc_factor)
            heading_author_gt_img = self.create_region_gt_img(self.heading_author_regions[i], img_width, img_height,
                                                              fill=True, scaling_factor=sc_factor)
            heading_other_gt_img = self.create_region_gt_img(self.heading_other_regions[i], img_width, img_height,
                                                             fill=True, scaling_factor=sc_factor)
            text_block_gt_img = self.create_region_gt_img(self.text_regions_list[i], img_width, img_height, fill=True,
                                                          scaling_factor=sc_factor)

            gt_channels = [table_gt_img, advert_gt_img, title_headline_gt_img, title_subheadline_gt_img,
                           title_publ_stmt_gt_img, title_other_gt_img, heading_title_gt_img, heading_overline_gt_img,
                           heading_subheadline_gt_img, heading_author_gt_img, heading_other_gt_img, text_block_gt_img]

            other_gt_img = self.create_other_ground_truth_image(*gt_channels)
            gt_channels.append(other_gt_img)
            gt_channels = tuple(gt_channels)

            self.gt_imgs_lst.append(gt_channels)
        self.make_disjoint_all()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image_list', type=str)
    parser.add_argument('--save_dir', type=str)
    parser.add_argument('--fixed_height', type=int, default=0)
    parser.add_argument('--scaling_factor', type=float, default=1.0)

    args = parser.parse_args()

    tb_generator = LuxwortGroundTruthGenerator(
        args.image_list, use_bounding_box=False, use_min_area_rect=False, fixed_height=args.fixed_height,
        scaling_factor=args.scaling_factor)

    tb_generator.run_ground_truth_generation(args.save_dir)