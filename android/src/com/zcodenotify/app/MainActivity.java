package com.zcodenotify.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.ValueCallback;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

public class MainActivity extends Activity {

    private WebView web;
    private View setupView;
    private TextView statusText;
    private EditText urlInput;

    /** 手机通知页在前台时为 true，前台服务据此抑制重复的系统通知。 */
    public static volatile boolean pageVisible = false;

    private volatile boolean searching = false;
    private long lastAutoSearch = 0;
    /** 网页请求摄像头（扫码）时若运行时权限未给，先存请求，授权后补 grant。 */
    private volatile PermissionRequest pendingCamRequest;
    /** 网页 <input type=file>（扫码拍照）的回调，onActivityResult 回传。 */
    private ValueCallback<Uri[]> fileUploadCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Android 13+ 通知权限需要运行时申请
        if (Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                        != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }

        web = new WebView(this);
        web.getSettings().setJavaScriptEnabled(true);
        web.getSettings().setDomStorageEnabled(true);
        web.setBackgroundColor(0xFF0B0E14);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req != null && req.isForMainFrame()) {
                    runOnUiThread(new Runnable() {
                        public void run() { reSearchFromError(); }
                    });
                }
            }
        });
        web.addJavascriptInterface(new Bridge(), "AndroidNotify");
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(new Runnable() {
                    public void run() {
                        if (checkSelfPermission("android.permission.CAMERA")
                                == PackageManager.PERMISSION_GRANTED) {
                            request.grant(request.getResources());
                        } else {
                            pendingCamRequest = request;
                            requestPermissions(new String[]{"android.permission.CAMERA"}, 2);
                        }
                    }
                });
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (fileUploadCallback != null) fileUploadCallback.onReceiveValue(null);
                fileUploadCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), 100);
                } catch (Exception e) {
                    fileUploadCallback = null;
                    return false;
                }
                return true;
            }
        });

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFF0B0E14);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setupView = buildSetupView();
        root.addView(setupView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        startLoopbackForward();   // WebView 从 127.0.0.1 加载页面才是安全上下文，扫码才能开实时摄像头
        refresh();
        NotifyService.start(this);
    }

    // ------------------------------------------------- 本地回环转发（扫码的前提）

    /** WebView 侧固定走这个端口的回环地址，每个连接动态转发到 prefs 里存的电脑地址。 */
    private static final int LOOPBACK_PORT = 8790;

    /** 在 127.0.0.1:8790 起一个 TCP 转发器：http://127.0.0.1 属安全上下文，网页才能 getUserMedia。 */
    private void startLoopbackForward() {
        new Thread(new Runnable() {
            public void run() {
                try {
                    java.net.ServerSocket ss = new java.net.ServerSocket(
                            LOOPBACK_PORT, 50, java.net.InetAddress.getByName("127.0.0.1"));
                    while (true) {
                        final java.net.Socket client = ss.accept();
                        new Thread(new Runnable() {
                            public void run() { pump(client); }
                        }).start();
                    }
                } catch (Exception e) {
                    // 端口被占说明上一个实例的转发器还在，直接复用即可
                }
            }
        }, "loopback-forward").start();
    }

    /** 单个回环连接 -> 电脑 8787：双向搬运，每块即刷（SSE 长连接依赖）。 */
    private void pump(java.net.Socket client) {
        java.net.Socket remote = null;
        try {
            URL u = new URL(prefs().getString("url", ""));
            remote = new java.net.Socket();
            remote.connect(new java.net.InetSocketAddress(u.getHost(),
                    u.getPort() > 0 ? u.getPort() : 80), 3000);
            final java.net.Socket r = remote;
            Thread up = new Thread(new Runnable() { public void run() { copy(client, r); } });
            Thread down = new Thread(new Runnable() { public void run() { copy(r, client); } });
            up.start(); down.start();
            up.join(); down.join();
        } catch (Exception e) {
        } finally {
            try { client.close(); } catch (Exception e2) {}
            if (remote != null) try { remote.close(); } catch (Exception e2) {}
        }
    }

    private static void copy(java.net.Socket in, java.net.Socket out) {
        byte[] buf = new byte[8192];
        try {
            java.io.InputStream is = in.getInputStream();
            java.io.OutputStream os = out.getOutputStream();
            int n;
            while ((n = is.read(buf)) != -1) {
                os.write(buf, 0, n);
                os.flush();
            }
        } catch (Exception e) {
        } finally {
            try { in.close(); } catch (Exception e2) {}
            try { out.close(); } catch (Exception e2) {}
        }
    }

    @Override
    protected void onResume() { super.onResume(); pageVisible = true; }

    @Override
    protected void onPause() { super.onPause(); pageVisible = false; }

    @Override
    public void onBackPressed() {
        moveTaskToBack(true);   // 退到后台但保持前台服务收通知
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        if (requestCode == 2 && grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED
                && pendingCamRequest != null) {
            pendingCamRequest.grant(pendingCamRequest.getResources());
            pendingCamRequest = null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == 100 && fileUploadCallback != null) {
            fileUploadCallback.onReceiveValue(
                    WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileUploadCallback = null;
        }
    }

    /** 网页用它告诉原生层“我现在可见/不可见”，避免 app 开着时再弹一遍系统通知。 */
    private class Bridge {
        @JavascriptInterface
        public void visible(boolean v) {
            MainActivity.pageVisible = v;
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("zcodenotify", MODE_PRIVATE);
    }

    private void refresh() {
        String url = prefs().getString("url", "");
        if (url.length() > 0) {
            setupView.setVisibility(View.GONE);
            // 页面从回环转发加载（127.0.0.1 为安全上下文）：prefs 里存的仍是真实电脑地址，
            // 转发器逐连接动态读取它，电脑 IP 变了重连即恢复
            web.loadUrl("http://127.0.0.1:" + LOOPBACK_PORT + "/phone");
        } else {
            setupView.setVisibility(View.VISIBLE);
            autoSearch();
        }
    }

    // ------------------------------------------------------------ 自动搜索电脑

    private void autoSearch() {
        if (searching) return;
        searching = true;
        lastAutoSearch = System.currentTimeMillis();
        statusText.setText("正在搜索同一 Wi-Fi 里的通知服务…");
        new Thread(new Runnable() {
            public void run() {
                List<String> urls = discover();
                if (urls.isEmpty()) {   // UDP 广播常被路由器/系统拦：直连上次地址再试一次
                    String again = probeSaved(prefs().getString("url", ""));
                    if (again != null) urls.add(again);
                }
                final List<String> found = urls;
                runOnUiThread(new Runnable() {
                    public void run() { searchDone(found); }
                });
            }
        }).start();
    }

    /** 从已存 url 提取 ip:port 再 probe 一次（TCP 直连通常比 UDP 广播可靠）。 */
    private static String probeSaved(String url) {
        try {
            URL u = new URL(url);
            return probe(u.getHost(), u.getPort() > 0 ? u.getPort() : 80);
        } catch (Exception e) {
            return null;
        }
    }

    /** 页面加载失败（电脑关机/IP 变了）时自动重新搜索，带 5s 防抖。 */
    private void reSearchFromError() {
        setupView.setVisibility(View.VISIBLE);
        if (searching) return;
        if (System.currentTimeMillis() - lastAutoSearch < 5000) {
            statusText.setText("连不上服务器（电脑关机了或 IP 变了）。\n"
                    + "启动电脑端服务后，点下面的按钮重新搜索。");
        } else {
            autoSearch();   // 内含 UDP 广播 + 直连上次地址双重探测
        }
    }

    /** 广播找电脑 + 逐个探测，返回可用的 /phone 地址（网络操作，须在子线程）。 */
    private List<String> discover() {
        List<String> found = new ArrayList<String>();
        Set<String> seen = new HashSet<String>();
        DatagramSocket s = null;
        try {
            s = new DatagramSocket();
            s.setBroadcast(true);
            byte[] q = "ZCODE-NOTIFY-DISCOVER".getBytes("UTF-8");
            List<InetAddress> bcast = new ArrayList<InetAddress>();
            try { bcast.add(InetAddress.getByName("255.255.255.255")); } catch (Exception ignored) {}
            try {
                for (NetworkInterface nif : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                    if (!nif.isUp() || nif.isLoopback()) continue;
                    for (InterfaceAddress ia : nif.getInterfaceAddresses()) {
                        InetAddress b = ia.getBroadcast();
                        if (b != null && !bcast.contains(b)) bcast.add(b);
                    }
                }
            } catch (Exception ignored) {}
            for (InetAddress b : bcast) {
                try { s.send(new DatagramPacket(q, q.length, b, 8788)); } catch (Exception ignored) {}
            }
            byte[] buf = new byte[2048];
            long deadline = System.currentTimeMillis() + 2500;
            while (System.currentTimeMillis() < deadline) {
                try {
                    s.setSoTimeout((int) Math.max(100, deadline - System.currentTimeMillis()));
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    s.receive(p);
                    JSONObject o = new JSONObject(new String(p.getData(), 0, p.getLength(), "UTF-8"));
                    int port = o.optInt("port", 8787);
                    JSONArray ips = o.optJSONArray("ips");
                    if (ips != null) {
                        for (int i = 0; i < ips.length(); i++) {
                            String url = probe(ips.optString(i), port);
                            if (url != null && seen.add(url)) found.add(url);
                        }
                    }
                } catch (java.net.SocketTimeoutException ignored) {
                } catch (Exception ignored) {}
            }
        } catch (Exception ignored) {
        } finally {
            if (s != null) s.close();
        }
        return found;
    }

    private static String probe(String ip, int port) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://" + ip + ":" + port + "/status").openConnection();
            c.setConnectTimeout(1200);
            c.setReadTimeout(1200);
            if (c.getResponseCode() == 200) return "http://" + ip + ":" + port + "/phone";
        } catch (Exception ignored) {
        } finally {
            if (c != null) c.disconnect();
        }
        return null;
    }

    private void searchDone(List<String> urls) {
        searching = false;
        if (isFinishing() || isDestroyed()) return;
        if (urls.size() == 1) {
            prefs().edit().putString("url", urls.get(0)).apply();
            Toast.makeText(this, "已自动连接电脑通知服务", Toast.LENGTH_SHORT).show();
            refresh();
        } else if (urls.size() > 1) {
            final String[] items = urls.toArray(new String[0]);
            statusText.setText("发现多台电脑，选一台连接：");
            new AlertDialog.Builder(this)
                    .setTitle("发现多台电脑的通知服务")
                    .setItems(items, new DialogInterface.OnClickListener() {
                        public void onClick(DialogInterface d, int w) {
                            prefs().edit().putString("url", items[w]).apply();
                            refresh();
                        }
                    })
                    .setNegativeButton("手动输入", null)
                    .show();
        } else {
            statusText.setText("没自动找到电脑。请确认：电脑已双击 启动通知服务.bat、"
                    + "手机与电脑连同一个 Wi-Fi（首次要在防火墙弹窗里点允许）；"
                    + "或直接在下面粘贴地址。");
        }
    }

    // ------------------------------------------------------------ 首次配置页

    private View buildSetupView() {
        float d = getResources().getDisplayMetrics().density;
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(0xFF0B0E14);
        box.setPadding((int) (36 * d), (int) (80 * d), (int) (36 * d), 0);

        TextView title = new TextView(this);
        title.setText("ZCode 任务通知");
        title.setTextSize(28);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setTextColor(0xFFE8EDF6);
        box.addView(title);

        TextView sub = new TextView(this);
        sub.setText("电脑上 ZCode 一完成任务，\n手机立刻响铃提醒。");
        sub.setTextSize(15);
        sub.setTextColor(0xFF8B97AC);
        sub.setLineSpacing((int) (4 * d), 1f);
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp.topMargin = (int) (10 * d);
        box.addView(sub, sp);

        TextView steps = new TextView(this);
        steps.setText("打开本 app 会自动搜索并连接同一 Wi-Fi 里的电脑。\n"
                + "搜不到时（首次要在电脑防火墙弹窗里点允许）：\n"
                + "1. 电脑上双击 zcode-notify 里的 启动通知服务.bat\n"
                + "2. 或在电脑控制台复制「手机端地址」粘贴到下面");
        steps.setTextSize(13);
        steps.setTextColor(0xFF6F7B92);
        steps.setLineSpacing((int) (5 * d), 1f);
        LinearLayout.LayoutParams st = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        st.topMargin = (int) (36 * d);
        box.addView(steps, st);

        statusText = new TextView(this);
        statusText.setTextColor(0xFFFFAB8A);
        statusText.setTextSize(13);
        statusText.setLineSpacing((int) (4 * d), 1f);
        LinearLayout.LayoutParams sp2 = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        sp2.topMargin = (int) (24 * d);
        box.addView(statusText, sp2);

        urlInput = new EditText(this);
        urlInput.setHint("手动粘贴手机端地址（一般不用）");
        urlInput.setSingleLine(true);
        urlInput.setTextColor(0xFFE8EDF6);
        urlInput.setHintTextColor(0xFF566078);
        urlInput.setTextSize(13);
        urlInput.setBackground(rounded(0xFF1A212E, 0xFF2A3444, (int) (14 * d)));
        urlInput.setPadding((int) (16 * d), (int) (14 * d), (int) (16 * d), (int) (14 * d));
        LinearLayout.LayoutParams ip = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        ip.topMargin = (int) (18 * d);
        box.addView(urlInput, ip);

        Button go = new Button(this);
        go.setText("手动连接");
        go.setTextColor(Color.WHITE);
        go.setTextSize(16);
        go.setTypeface(Typeface.DEFAULT_BOLD);
        go.setAllCaps(false);
        GradientDrawable bg = new GradientDrawable(GradientDrawable.Orientation.TL_BR,
                new int[]{0xFFFF7A45, 0xFFFF6335});
        bg.setCornerRadius(14 * d);
        go.setBackground(bg);
        go.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { connect(); }
        });
        LinearLayout.LayoutParams gp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, (int) (52 * d));
        gp.topMargin = (int) (18 * d);
        box.addView(go, gp);

        Button rescan = new Button(this);
        rescan.setText("自动搜索电脑");
        rescan.setTextColor(0xFFC9D1E0);
        rescan.setTextSize(14);
        rescan.setAllCaps(false);
        rescan.setBackground(rounded(0xFF161D29, 0xFF2A3444, (int) (14 * d)));
        rescan.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { autoSearch(); }
        });
        LinearLayout.LayoutParams rp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, (int) (46 * d));
        rp.topMargin = (int) (12 * d);
        box.addView(rescan, rp);

        TextView tip = new TextView(this);
        tip.setText("手机需与电脑连同一个 Wi-Fi；连接成功后会以\n「ZCode 通知服务运行中」常驻后台接收提醒。");
        tip.setTextSize(12);
        tip.setTextColor(0xFF566078);
        tip.setLineSpacing((int) (4 * d), 1f);
        LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        tp.topMargin = (int) (22 * d);
        box.addView(tip, tp);

        return box;
    }

    private GradientDrawable rounded(int fill, int stroke, int radiusPx) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(radiusPx);
        g.setStroke(2, stroke);
        return g;
    }

    private void connect() {
        String u = urlInput.getText().toString().trim();
        if (u.length() == 0) {
            Toast.makeText(this, "先粘贴电脑控制台上的「手机端地址」，或点自动搜索", Toast.LENGTH_LONG).show();
            return;
        }
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://" + u;
        u = u.replaceAll("/+$", "");
        if (!u.contains("/phone")) u += "/phone";
        prefs().edit().putString("url", u).apply();
        refresh();
    }
}
