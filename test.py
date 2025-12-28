import cv2
import time
import numpy as np
from ultralytics import YOLO

# Load lightweight & accurate model
model = YOLO("yolov5n.pt")  # Best balance for accuracy + speed

# Open local camera (0 = default webcam)
cap = cv2.VideoCapture(0)

# Camera resolution (lower = faster)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

prev_centers = []
fight_alert = False

while True:
    ret, frame = cap.read()
    if not ret:
        break

    # Run detection
    results = model(frame, conf=0.45, classes=[0])  # person only

    centers = []
    people_count = 0

    for r in results:
        for box in r.boxes.xyxy.cpu().numpy():
            x1, y1, x2, y2 = map(int, box)
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2

            centers.append((cx, cy))
            people_count += 1

            # Draw bounding box
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

    # ---- Crowd & Fight Heuristic ----
    fight_alert = False
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            if abs(centers[i][0] - centers[j][0]) < 50 and \
               abs(centers[i][1] - centers[j][1]) < 50:
                fight_alert = True

    # ---- Display Info ----
    cv2.putText(frame, f"People: {people_count}",
                (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1,
                (255, 255, 0), 2)

    if fight_alert:
        cv2.putText(frame, "ALERT: CLOSE CONTACT",
                    (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1,
                    (0, 0, 255), 3)

    cv2.imshow("Human Surveillance (Local Camera)", frame)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
