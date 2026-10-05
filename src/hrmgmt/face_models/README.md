face_detection_yunet_2023mar.onnx is YuNet, a small face detector (about 230 KB), from the OpenCV Zoo
(https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet), MIT licence, by Shiqi Yu et al.
sha256 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
It only says whether a face is in the picture. It does not identify or compare anyone.

face_recognition_sface_2021dec_int8.onnx is SFace (8-bit version, about 10 MB), a face-matching model from the
OpenCV Zoo (https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface), Apache-2.0 licence
(LICENSE-sface.txt), by Yaoyao Zhong et al. (https://arxiv.org/abs/2205.12010).
sha256 2b0e941e6f16cc048c20aee0c8e31f569118f65d702914540f7bfdc14048d78a
It turns a face into 128 numbers; two photos of the same person give numbers that are close. It is used only to
flag a check-in or check-out photo whose face does not match the employee's profile photo (or, when there is no
profile photo, the check-in photo of the same day). It never blocks anyone. The numbers are not a photo, but they
are personal data: they are deleted with the employee.
