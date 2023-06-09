#!/usr/bin/env python

import numpy as np
#import cv2
import rospy
import tf
import tf2_ros
from std_msgs.msg import Header, String
from sensor_msgs.msg import Image as ImageMsg
from sensor_msgs.msg import Imu, CameraInfo
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Pose, PoseStamped, Point, \
     PointStamped, TransformStamped, Twist, Quaternion
from rosgraph_msgs.msg import Clock
from cv_bridge import CvBridge, CvBridgeError

import tesse_ros_bridge.utils

from tesse_ros_bridge.srv import SceneRequestService, \
     ObjectSpawnRequestService
from tesse_ros_bridge import brh_T_blh

from tesse.msgs import *
from tesse.env import *
from tesse.utils import *

class TesseROSWrapper:

    def __init__(self):
        # Networking parameters:
        self.client_ip     = rospy.get_param("~client_ip", "127.0.0.1")
        self.self_ip       = rospy.get_param("~self_ip", "127.0.0.1")
        self.position_port = rospy.get_param("~position_port", 19000)
        self.metadata_port = rospy.get_param("~metadata_port", 19001)
        self.image_port    = rospy.get_param("~image_port", 19002)
        self.udp_port      = rospy.get_param("~udp_port", 19004)
        self.step_port     = rospy.get_param("~step_port", 19005)

        # Set data to publish
        # `publish_mono_stereo` is true to publish one channel stereo images
        # Otherwise, publish as bgr8
        publish_mono_stereo    = rospy.get_param("~publish_mono_stereo", True)
        publish_segmentation   = rospy.get_param("~publish_segmentation", True)
        publish_depth          = rospy.get_param("~publish_depth", True)
        self.publish_metadata  = rospy.get_param("~publish_metadata", False)

        # Camera parameters:
        self.camera_width    = rospy.get_param("~camera_width", 720)
        assert(self.camera_width > 0)
        assert(self.camera_width % 2 == 0)
        self.camera_height   = rospy.get_param("~camera_height", 480)
        assert(self.camera_height > 0)
        assert(self.camera_height % 2 == 0)
        self.camera_fov      = rospy.get_param("~camera_vertical_fov", 60)
        assert(self.camera_fov > 0)
        self.stereo_baseline = rospy.get_param("~stereo_baseline", 0.2)
        assert(self.stereo_baseline > 0)

        # Near and far draw distances determine Unity camera rendering bounds.
        self.near_draw_dist  = rospy.get_param("~near_draw_dist", 0.05)
        self.far_draw_dist   = rospy.get_param("~far_draw_dist", 50)

        # Simulator speed parameters:
        self.speedup_factor = rospy.get_param("~speedup_factor", 1.0)
        assert(self.speedup_factor > 0.0)  # We are  dividing by this so > 0
        self.frame_rate     = rospy.get_param("~frame_rate", 20.0)
        self.imu_rate       = rospy.get_param("~imu_rate", 200.0)

        # Output parameters:
        self.world_frame_id     = rospy.get_param("~world_frame_id", "world")
        self.body_frame_id      = rospy.get_param("~body_frame_id", "base_link_gt")
        self.left_cam_frame_id  = rospy.get_param("~left_cam_frame_id", "left_cam")
        self.right_cam_frame_id = rospy.get_param("~right_cam_frame_id", "right_cam")
        assert(self.left_cam_frame_id != self.right_cam_frame_id)

        self.env = Env(simulation_ip=self.client_ip,
                       own_ip=self.self_ip,
                       position_port=self.position_port,
                       metadata_port=self.metadata_port,
                       image_port=self.image_port,
                       step_port=self.step_port)

        # To send images via ROS network and convert from/to ROS
        self.cv_bridge = CvBridge()

        # publish left and right cameras as mono8 or bgr8, depending on the given param
        n_stereo_channels = Channels.SINGLE if publish_mono_stereo else Channels.THREE
        self.cameras=[(Camera.RGB_LEFT,  Compression.OFF, n_stereo_channels, self.left_cam_frame_id),
                      (Camera.RGB_RIGHT, Compression.OFF, n_stereo_channels, self.right_cam_frame_id)]

        self.img_pubs = [rospy.Publisher("left_cam/rgb/image_raw", ImageMsg, queue_size=10),
                         rospy.Publisher("right_cam/rgb/image_raw", ImageMsg, queue_size=10)]

        # setup optional publishers
        if publish_segmentation:
            self.cameras.append((Camera.SEGMENTATION, Compression.OFF, Channels.THREE,  self.left_cam_frame_id))
#            self.img_pubs.append(rospy.Publisher("segmentation/image_raw", ImageMsg, queue_size=10))
	    self.img_pubs.append(rospy.Publisher("seg_cam/rgb/image_raw", ImageMsg, queue_size=10))

        if publish_depth:
            self.cameras.append((Camera.DEPTH, Compression.OFF, Channels.THREE,  self.left_cam_frame_id))
#            self.img_pubs.append(rospy.Publisher("depth/image_raw", ImageMsg, queue_size=10))
            self.img_pubs.append(rospy.Publisher("depth_cam/mono/image_raw", ImageMsg, queue_size=10))

        if self.publish_metadata:
            self.metadata_pub = rospy.Publisher("metadata", String)

        # Camera information members.
        # TODO(marcus): reformat like img_pubs
#        self.cam_info_pubs = [rospy.Publisher("left_cam/camera_info",     CameraInfo, queue_size=10),
#                              rospy.Publisher("right_cam/camera_info",    CameraInfo, queue_size=10),
#                              rospy.Publisher("segmentation/camera_info", CameraInfo, queue_size=10),
#                              rospy.Publisher("depth/camera_info",        CameraInfo, queue_size=10)]

        self.cam_info_pubs = [rospy.Publisher("left_cam/camera_info",     CameraInfo, queue_size=10),
                              rospy.Publisher("right_cam/camera_info",    CameraInfo, queue_size=10),
                              rospy.Publisher("seg_cam/camera_info", CameraInfo, queue_size=10),
                              rospy.Publisher("depth_cam/camera_info",        CameraInfo, queue_size=10)]

        self.cam_info_msgs = []

        # If the clock updates faster than images can be queried in
        # step mode, the image callback is called twice on the same
        # timestamp which leads to duplicate published images.
        # Track image timestamps to prevent this
        self.last_image_timestamp = None

        # Setup ROS publishers
        self.imu_pub  = rospy.Publisher("imu", Imu, queue_size=10)
        self.odom_pub = rospy.Publisher("odom", Odometry, queue_size=10)

        # Setup ROS services.
        self.setup_ros_services()

        # Transform broadcasters.
        self.tf_broadcaster = tf.TransformBroadcaster()
        # Don't call static_tf_broadcaster.sendTransform multiple times.
        # Rather call it once with multiple static tfs! Check issue #40
        self.static_tf_broadcaster = tf2_ros.StaticTransformBroadcaster()

        # Required states for finite difference calculations.
        self.prev_time      = 0.0
        self.prev_vel_brh   = [0.0, 0.0, 0.0]
        self.prev_enu_R_brh = np.identity(3)

        # Setup camera parameters and extrinsics in the simulator per spec.
        self.setup_cameras()

        # Setup collision
        enable_collision = rospy.get_param("~enable_collision", 0)
        self.setup_collision(enable_collision)

        # Change scene
        initial_scene = rospy.get_param("~initial_scene", 2)
        rospy.wait_for_service('scene_change_request')
        self.change_scene(initial_scene)
        print(initial_scene)

        # Setup UdpListener.
        self.udp_listener = UdpListener(port=self.udp_port, rate=self.imu_rate)
        self.udp_listener.subscribe('udp_subscriber', self.udp_cb)

        # Simulated time requires that we constantly publish to '/clock'.
        self.clock_pub = rospy.Publisher("/clock", Clock, queue_size=10)

        # Setup simulator step mode
        step_mode_enabled = rospy.get_param("~enable_step_mode", False)
        if step_mode_enabled:
            self.env.send(SetFrameRate(self.frame_rate))

        print("TESSE_ROS_NODE: Initialization complete.")

    def spin(self):
        """ Start timers and callbacks.

            Because we are publishing sim time, we
            cannot simply call `rospy.spin()` as this will wait for messages
            to go to /clock first, and will freeze the node.
        """
        rospy.Timer(rospy.Duration(1.0 / self.frame_rate), self.image_cb)
        self.udp_listener.start()

        # rospy.spin()

        while not rospy.is_shutdown():
            self.clock_cb(None)

    def udp_cb(self, data):
        """ Callback for UDP metadata at high rates.

            Parses raw metadata from the simulator, processes it into the
            proper reference frame, and publishes it as odometry, imu and
            transform information to ROS.

            Args:
                data: A string or bytestring in xml format containing the
                    metadata from the simulator.
        """
        # Parse metadata and process for proper use.
        metadata = tesse_ros_bridge.utils.parse_metadata(data)
        metadata_processed = tesse_ros_bridge.utils.process_metadata(metadata,
            self.prev_time, self.prev_vel_brh, self.prev_enu_R_brh)

        assert(self.prev_time < metadata_processed['time'])
        self.prev_time      = metadata_processed['time']
        self.prev_vel_brh   = metadata_processed['velocity']
        self.prev_enu_R_brh = metadata_processed['transform'][:3,:3]

        timestamp = rospy.Time.from_sec(
            metadata_processed['time'] / self.speedup_factor)

        # Publish simulated time.
        # TODO(marcus): decide who should publish timestamps
        # self.clock_pub.publish(timestamp)

        # Publish imu and odometry messages.
        imu = tesse_ros_bridge.utils.metadata_to_imu(metadata_processed,
            timestamp, self.body_frame_id)
        self.imu_pub.publish(imu)
        odom = tesse_ros_bridge.utils.metadata_to_odom(metadata_processed,
            timestamp, self.world_frame_id, self.body_frame_id)
        self.odom_pub.publish(odom)

        # Publish agent ground truth transform.
        self.publish_tf(metadata_processed['transform'], timestamp)

    def image_cb(self, event):
        """ Publish images from simulator to ROS.

            Left and right images are published in the mono8 encoding.
            Depth images are pre-processed s.t. pixel values directly give
            point depth, in meters.
            Segmentation images are published in the rgb8 encoding.

            Args:
                event: A rospy.Timer event object, which is not used in this
                    method. You may supply `None`.
        """
        try:
            # Get camera data.
            data_response = self.env.request(DataRequest(True, self.cameras))

            # Process metadata to publish transform.
            metadata = tesse_ros_bridge.utils.parse_metadata(
                data_response.metadata)

            timestamp = rospy.Time.from_sec(
                metadata['time'] / self.speedup_factor)

            if timestamp == self.last_image_timestamp:
                rospy.loginfo("Skipping duplicate images at timestamp %s" % self.last_image_timestamp)
                return

            # self.clock_pub.publish(timestamp)

            # Process each image.
            for i in range(len(self.cameras)):
                if self.cameras[i][0] == Camera.DEPTH:
                    img_msg = self.cv_bridge.cv2_to_imgmsg(
                        data_response.images[i] * self.far_draw_dist,
                            'passthrough')
                elif self.cameras[i][2] == Channels.SINGLE:
                    img_msg = self.cv_bridge.cv2_to_imgmsg(
                        data_response.images[i], 'mono8')
                elif self.cameras[i][2] == Channels.THREE:
                    img_msg = self.cv_bridge.cv2_to_imgmsg(
                        data_response.images[i], 'rgb8') # [:,:,::-1]

                # Sanity check resolutions.
                assert(img_msg.width == self.cam_info_msgs[i].width)
                assert(img_msg.height == self.cam_info_msgs[i].height)

                # Publish images to appropriate topic.
                img_msg.header.frame_id = self.cameras[i][3]
                img_msg.header.stamp = timestamp
                self.img_pubs[i].publish(img_msg)

                # Publish associated CameraInfo message.
                self.cam_info_msgs[i].header.stamp = timestamp
                self.cam_info_pubs[i].publish(self.cam_info_msgs[i])

            self.publish_tf(
                tesse_ros_bridge.utils.get_enu_T_brh(metadata),
                    timestamp)

            if self.publish_metadata:
                self.metadata_pub.publish(data_response.metadata)

            self.last_image_timestamp = timestamp

        except Exception as error:
                print "TESSE_ROS_NODE: image_cb error: ", error

    def clock_cb(self, event):
        """ Publishes simulated clock time.

            Gets current metadata from the simulator over the low-rate metadata
            port. Publishes the timestamp, optionally modified by the
            specified speedup_factor.

            Args:
                event: A rospy.Timer event object, which is not used in this
                    method. You may supply `None`.
        """
        try:
            metadata = tesse_ros_bridge.utils.parse_metadata(self.env.request(
                MetadataRequest()).metadata)

            sim_time = rospy.Time.from_sec(
                metadata['time'] / self.speedup_factor)
            self.clock_pub.publish(sim_time)
        except Exception as error:
            print "TESSE_ROS_NODE: clock_cb error: ", error

    def setup_cameras(self):
        """ Initializes image-related members.

            Sends camera parameter, position and rotation data to the simulator
            to properly reset them as specified in the node arguments.
            Calculates and sends static transforms for the left and
            right cameras relative to the body frame.
            Also constructs the CameraInfo messages for left and right cameras,
            to be published with every frame.
        """
        # Set camera parameters once for the entire simulation.
        # Set all cameras to have same intrinsics:
        for camera in self.cameras:
            camera_id = camera[0]
            if camera_id is not Camera.THIRD_PERSON:
                resp = None
                while resp is None:
                    print("TESSE_ROS_NODE: Setting intrinsic parameters for camera: ",
                        camera_id)
                    resp = self.env.request(SetCameraParametersRequest(
                        camera_id,
                        self.camera_height,
                        self.camera_width,
                        self.camera_fov,
                        self.near_draw_dist,
                        self.far_draw_dist
                        ))

        # TODO(marcus): add SetCameraOrientationRequest option.
        # TODO(Toni): this is hardcoded!! what if don't want IMU in the middle?
        # Also how is this set using x? what if it is y, z?
        left_cam_position  = Point(x = -self.stereo_baseline / 2,
                                   y = 0.0,
                                   z = 0.0)
        right_cam_position = Point(x = self.stereo_baseline / 2,
                                   y = 0.0,
                                   z = 0.0)
        cameras_orientation = Quaternion(x=0.0,
                                         y=0.0,
                                         z=0.0,
                                         w=1.0)

        resp = None
        while resp is None:
            print "TESSE_ROS_NODE: Setting position of left camera..."
            resp = self.env.request(SetCameraPositionRequest(
                    Camera.RGB_LEFT,
                    left_cam_position.x,
                    left_cam_position.y,
                    left_cam_position.z,
                    ))

        resp = None
        while resp is None:
            print "TESSE_ROS_NODE: Setting position of right camera..."
            resp = self.env.request(SetCameraPositionRequest(
                    Camera.RGB_RIGHT,
                    right_cam_position.x,
                    right_cam_position.y,
                    right_cam_position.z,
                    ))

        # Set position depth and segmentation cameras to align with left:
        resp = None
        while resp is None:
            print "TESSE_ROS_NODE: Setting position of depth camera..."
            resp = self.env.request(SetCameraPositionRequest(
                    Camera.DEPTH,
                    left_cam_position.x,
                    left_cam_position.y,
                    left_cam_position.z,
                    ))
        resp = None
        while resp is None:
            print "TESSE_ROS_NODE: Setting position of segmentation camera..."
            resp = self.env.request(SetCameraPositionRequest(
                    Camera.SEGMENTATION,
                    left_cam_position.x,
                    left_cam_position.y,
                    left_cam_position.z,
                    ))

        for camera in self.cameras:
            camera_id = camera[0]
            if camera_id is not Camera.THIRD_PERSON:
                resp = None
                while resp is None:
                    print("TESSE_ROS_NODE: Setting orientation of all cameras to identity...")
                    resp = self.env.request(SetCameraOrientationRequest(
                            camera_id,
                            cameras_orientation.x,
                            cameras_orientation.y,
                            cameras_orientation.z,
                            cameras_orientation.w,
                            ))

        # Left cam static tf.
        static_tf_cam_left                       = TransformStamped()
        static_tf_cam_left.header.frame_id       = self.body_frame_id
        static_tf_cam_left.header.stamp          = rospy.Time.now()
        static_tf_cam_left.transform.translation = left_cam_position
        static_tf_cam_left.transform.rotation    = cameras_orientation
        static_tf_cam_left.child_frame_id        = self.left_cam_frame_id

        # Right cam static tf.
        static_tf_cam_right                       = TransformStamped()
        static_tf_cam_right.header.frame_id       = self.body_frame_id
        static_tf_cam_right.header.stamp          = rospy.Time.now()
        static_tf_cam_right.transform.translation = right_cam_position
        static_tf_cam_right.transform.rotation    = cameras_orientation
        static_tf_cam_right.child_frame_id        = self.right_cam_frame_id

        # Send static tfs over the ROS network
        self.static_tf_broadcaster.sendTransform([static_tf_cam_right, static_tf_cam_left])

        # Camera_info publishing for VIO.
        left_cam_data = None
        while left_cam_data is None:
            print("TESSE_ROS_NODE: Acquiring left camera data...")
            left_cam_data = tesse_ros_bridge.utils.parse_cam_data(
                self.env.request(
                    CameraInformationRequest(Camera.RGB_LEFT)).metadata)
            assert(left_cam_data['id'] == 0)
            assert(left_cam_data['parameters']['height'] > 0)
            assert(left_cam_data['parameters']['width'] > 0)

        right_cam_data = None
        while right_cam_data is None:
            print("TESSE_ROS_NODE: Acquiring right camera data...")
            right_cam_data = tesse_ros_bridge.utils.parse_cam_data(
                self.env.request(
                    CameraInformationRequest(Camera.RGB_RIGHT)).metadata)
            assert(right_cam_data['id'] == 1)
            assert(left_cam_data['parameters']['height'] > 0)
            assert(left_cam_data['parameters']['width'] > 0)

        assert(left_cam_data['parameters']['height'] == self.camera_height)
        assert(left_cam_data['parameters']['width']  == self.camera_width)
        assert(right_cam_data['parameters']['height'] == self.camera_height)
        assert(right_cam_data['parameters']['width']  == self.camera_width)

        cam_info_msg_left, cam_info_msg_right = \
            tesse_ros_bridge.utils.generate_camera_info(
                left_cam_data, right_cam_data)
        
        # TODO(Toni) we should extend the above to get camera info for depth and segmentation!
        # for now, just copy paste from left cam...
        cam_info_msg_segmentation = cam_info_msg_left
        cam_info_msg_depth = cam_info_msg_left

        # TODO(Toni): do a check here by requesting all camera info and checking that it is
        # as the one requested!
        self.cam_info_msgs = [cam_info_msg_left,
                              cam_info_msg_right,
                              cam_info_msg_segmentation,
                              cam_info_msg_depth]

    def setup_ros_services(self):
        """ Setup ROS services related to the simulator.

            These services include:
                scene_change_request: change the scene_id of the simulator
                object_spawn_request: spawn a prefab object into the scene
        """
        self.scene_request_service = rospy.Service("scene_change_request",
                                                    SceneRequestService,
                                                    self.rosservice_change_scene)
        self.change_scene = rospy.ServiceProxy('scene_change_request',
                                               SceneRequestService)

        self.object_spawn_service = rospy.Service("object_spawn_request",
                                                  ObjectSpawnRequestService,
                                                  self.rosservice_spawn_object)
        self.spawn_object = rospy.ServiceProxy('object_spawn_request',
                                               ObjectSpawnRequestService)

    def setup_collision(self, enable_collision):
        """ Enable/Disable collisions in Simulator. """
        print("TESSE_ROS_NODE: Setup collisions to:", enable_collision)
        if enable_collision is True:
            self.env.send(ColliderRequest(enable=1))
        else:
            self.env.send(ColliderRequest(enable=0))

    def rosservice_change_scene(self, req):
        """ Change scene ID of simulator as a ROS service. """
        try:
            self.env.request(SceneRequest(req.id))
            return True
        except Exception as e:
            print("Scene Change Error: ", e)
        
        return False

    def rosservice_spawn_object(self, req):
        """ Spawn an object into the simulator as a ROS service. """
        type_switcher = {
            0: ObjectType.CUBE,
            1: ObjectType.SMPL_F_AUTO,
            2: ObjectType.SMPL_M_AUTO,
        }

        try:
            if req.pose == Pose():
                self.env.request(SpawnObjectRequest(type_switcher[req.id],
                                                        ObjectSpawnMethod.RANDOM))
            else:
                self.env.request(SpawnObjectRequest(type_switcher[req.id],
                                                        ObjectSpawnMethod.USER,
                                                        req.pose.position.x,
                                                        req.pose.position.y,
                                                        req.pose.position.z,
                                                        req.pose.orientation.x,
                                                        req.pose.orientation.y,
                                                        req.pose.orientation.z,
                                                        req.pose.orientation.w))
            return True
        except Exception as e:
            print("Object Spawn Error: ", e)
        
        return False

    def publish_tf(self, cur_tf, timestamp):
        """ Publish the ground-truth transform to the TF tree.

            Args:
                cur_tf: A 4x4 numpy matrix containing the transformation from
                    the body frame of the agent to ENU.
                timestamp: A rospy.Time instance representing the current
                    time in the simulator.
        """
        # Publish current transform to tf tree.
        trans = tesse_ros_bridge.utils.get_translation_part(cur_tf)
        quat = tesse_ros_bridge.utils.get_quaternion(cur_tf)
        self.tf_broadcaster.sendTransform(trans, quat, timestamp,
                                          self.body_frame_id,
                                          self.world_frame_id)


if __name__ == '__main__':
    rospy.init_node("TesseROSWrapper_node")
    node = TesseROSWrapper()
    node.spin()
