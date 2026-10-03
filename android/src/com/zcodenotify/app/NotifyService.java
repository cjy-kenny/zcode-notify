package com.zcodenotify.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import org.eclipse.paho.client.mqttv3.IMqttDeliveryToken;
import org.eclipse.paho.client.mqttv3.MqttCallbackExtended;
import org.eclipse.paho.client.mqttv3.MqttClient;
import org.eclipse.paho.client.mqttv3.MqttConnectOptions;
import org.eclipse.paho.client.mqttv3.MqttMessage;
import org.eclipse.paho.client.mqttv3.persist.MemoryPersistence;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/** 前台服务：常驻一条 SSE 连接，ZCode 一完成任务就弹系统通知（app 在后台也能收到）。 */
public class NotifyService extends Service {

    private static final String PREF = "zcodenotify";
    private static final String CH_TASK = "zcode_task";
    private static final String CH_SVC = "zcode_service";
    private static final int SVC_NOTIF_ID = 1;
    private static final String BROKER_TLS = "ssl://broker.emqx.io:8883";  // 优先加密
    private static final String BROKER_TCP = "tcp://broker.emqx.io:1883";  // TLS 连不上自动回退

    private volatile boolean running = false;
    private Thread worker;
    private Thread mqttThread;
    private MqttClient mqttClient;

    /** SSE（局域网）和 MQTT（跨网络）双通道同一条通知会到两次：按 id 去重。 */
    private static final LinkedHashMap<String, Boolean> SEEN =
            new LinkedHashMap<String, Boolean>() {
                @Override
                protected boolean removeEldestEntry(Map.Entry<String, Boolean> eldest) {
                    return size() > 50;
                }
            };

    private static synchronized boolean markSeen(String id) {
        if (SEEN.containsKey(id)) return false;
        SEEN.put(id, Boolean.TRUE);
        return true;
    }

    public static void start(Context ctx) {
        Intent i = new Intent(ctx, NotifyService.class);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        channels();
        startForegroundCompat(buildSvcNotif());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(loop, "sse");
            worker.start();
        }
        startMqtt();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        try {
            if (mqttClient != null) mqttClient.disconnect();
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREF, MODE_PRIVATE);
    }

    /** 固定 clientId：持久会话（cleanSession=false）依赖它，换设备/重装才变。 */
    private String clientId() {
        SharedPreferences sp = prefs();
        String cid = sp.getString("mqtt_cid", "");
        if (cid.length() == 0) {
            cid = "zcode-ph-" + randomHex();
            sp.edit().putString("mqtt_cid", cid).apply();
        }
        return cid;
    }

    private void channels() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        NotificationChannel svc = new NotificationChannel(CH_SVC, "后台连接",
                NotificationManager.IMPORTANCE_MIN);
        svc.setDescription("保持与电脑通知服务的连接");
        nm.createNotificationChannel(svc);
        NotificationChannel task = new NotificationChannel(CH_TASK, "任务完成通知",
                NotificationManager.IMPORTANCE_HIGH);
        task.setDescription("ZCode 任务完成提醒");
        task.enableVibration(true);
        nm.createNotificationChannel(task);
    }

    private Notification buildSvcNotif() {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH_SVC)
                : new Notification.Builder(this);
        b.setContentTitle("ZCode 通知服务运行中")
                .setContentText("保持与电脑的连接，随时接收任务完成提醒")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true);
        return b.build();
    }

    private void startForegroundCompat(Notification n) {
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(SVC_NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(SVC_NOTIF_ID, n);
        }
    }

    private final Runnable loop = new Runnable() {
        @Override public void run() {
            while (running) {
                String base = prefs().getString("url", "");
                if (base.length() == 0) { sleep(4000); continue; }
                HttpURLConnection conn = null;
                try {
                    conn = (HttpURLConnection) new URL(toEventsUrl(base)).openConnection();
                    conn.setConnectTimeout(5000);
                    conn.setReadTimeout(0);   // 流式读取，一直挂着
                    BufferedReader r = new BufferedReader(new InputStreamReader(
                            conn.getInputStream(), StandardCharsets.UTF_8));
                    StringBuilder data = new StringBuilder();
                    String line;
                    while ((line = r.readLine()) != null) {
                        if (line.startsWith("data:")) {
                            data.append(line.substring(5).trim());
                        } else if (line.length() == 0 && data.length() > 0) {
                            handle(data.toString());
                            data.setLength(0);
                        }
                    }
                } catch (Exception e) {
                    // 电脑关了/断网：静默，等一会儿重连
                } finally {
                    if (conn != null) conn.disconnect();
                }
                if (running) sleep(3000);
            }
        }
    };

    /** MQTT 订阅线程：从电脑 /status 自取订阅码，连公网 broker 收跨网络推送。 */
    private void startMqtt() {
        if (mqttThread != null && mqttThread.isAlive()) return;
        mqttThread = new Thread(new Runnable() {
            public void run() {
                while (running) {
                    try {
                        if (mqttClient != null && mqttClient.isConnected()) {
                            sleep(10000);
                            continue;
                        }
                        String base = prefs().getString("url", "");
                        String topic = prefs().getString("mqtt", "");
                        if (base.length() == 0) { sleep(5000); continue; }
                        if (topic.length() == 0) {
                            // 电脑面板的 /status 自带订阅码，取一次存下即可
                            try {
                                String su = toEventsUrl(base).replace("/events", "/status");
                                JSONObject st = new JSONObject(httpGetText(su));
                                String t = st.optString("mqtt", "");
                                if (t.length() > 0) {
                                    prefs().edit().putString("mqtt", t).apply();
                                    topic = t;
                                }
                            } catch (Exception ignored) {
                            }
                            if (topic.length() == 0) { sleep(8000); continue; }
                        }
                        // MQTTS 优先、明文回退；持久会话让离线期间的 QoS1 消息上线后补投
                        boolean ok = false;
                        for (String uri : new String[]{BROKER_TLS, BROKER_TCP}) {
                            try {
                                if (mqttClient == null || !uri.equals(mqttClient.getServerURI())) {
                                    if (mqttClient != null) {
                                        try { mqttClient.close(); } catch (Exception ignored) {}
                                    }
                                    mqttClient = new MqttClient(uri, clientId(), new MemoryPersistence());
                                    mqttClient.setCallback(new MqttCallbackExtended() {
                                        public void connectComplete(boolean reconnect, String serverURI) {
                                            try {
                                                mqttClient.subscribe(prefs().getString("mqtt", ""), 1);
                                            } catch (Exception ignored) {
                                            }
                                        }

                                        public void connectionLost(Throwable e) {
                                        }

                                        public void messageArrived(String t, MqttMessage m) {
                                            handle(new String(m.getPayload(), StandardCharsets.UTF_8));
                                        }

                                        public void deliveryComplete(IMqttDeliveryToken token) {
                                        }
                                    });
                                }
                                MqttConnectOptions opts = new MqttConnectOptions();
                                opts.setCleanSession(false);
                                opts.setConnectionTimeout(8);
                                opts.setKeepAliveInterval(60);
                                opts.setAutomaticReconnect(true);
                                if (uri.startsWith("ssl://")) {
                                    opts.setSocketFactory(javax.net.ssl.SSLSocketFactory.getDefault());
                                }
                                mqttClient.connect(opts);
                                ok = true;
                                break;
                            } catch (Exception e) {
                                // 这条路不通（比如老设备 TLS 握手失败），试下一条
                            }
                        }
                        sleep(ok ? 10000 : 8000);   // 连上交给自动重连托管；没连上过会儿再试
                    } catch (Exception e) {
                        sleep(8000);    // 没网/没配好：过会儿再试
                    }
                }
            }
        }, "mqtt");
        mqttThread.start();
    }

    private static String httpGetText(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(3000);
        c.setReadTimeout(3000);
        try {
            BufferedReader r = new BufferedReader(
                    new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
            StringBuilder b = new StringBuilder();
            String line;
            while ((line = r.readLine()) != null) b.append(line);
            return b.toString();
        } finally {
            c.disconnect();
        }
    }

    private static String randomHex() {
        return Long.toHexString(System.nanoTime())
                + Long.toHexString(System.currentTimeMillis() % 100000);
    }

    /** http://ip:8787/phone?x=1 -> http://ip:8787/events */
    static String toEventsUrl(String base) {
        String u = base;
        int q = u.indexOf('?');
        if (q >= 0) u = u.substring(0, q);
        u = u.replaceAll("/+$", "");
        if (u.endsWith("/phone")) u = u.substring(0, u.length() - "/phone".length());
        return u + "/events";
    }

    private void handle(String json) {
        try {
            JSONObject o = new JSONObject(json);
            String id = o.optString("id", String.valueOf(System.currentTimeMillis()));
            if (!markSeen(id)) return;   // 双通道重复投递，丢弃第二条
            if ("sent".equals(o.optString("kind", ""))) {
                return;   // 自己发的消息气泡：只进网页，不弹系统通知
            }
            double ts = o.optDouble("ts", 0);
            if (ts > 0 && System.currentTimeMillis() / 1000.0 - ts > 1800) {
                return;   // 离线补投的过期消息（超过 30 分钟）不弹
            }
            String title = o.optString("title", "ZCode 通知");
            String body = o.optString("body", "");
            notifyTask(id, title, body);
        } catch (Exception ignored) {
        }
    }

    private void notifyTask(String id, String title, String body) {
        if (MainActivity.pageVisible) return;   // 用户正看着通知页：页面自己会响铃，不重复弹
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH_TASK)
                : new Notification.Builder(this);
        b.setContentTitle(title)
                .setContentText(body)
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setStyle(new Notification.BigTextStyle().bigText(body));
        if (Build.VERSION.SDK_INT < 26) {
            b.setDefaults(Notification.DEFAULT_SOUND | Notification.DEFAULT_VIBRATE)
                    .setPriority(Notification.PRIORITY_HIGH);
        }
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        try {
            nm.notify(id.hashCode(), b.build());
        } catch (Exception ignored) {
            // 通知权限没批：连接照常，只是不弹
        }
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    }
}
