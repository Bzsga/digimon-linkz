// Minimal engine shims; OAuth, LitJson and WebAPIJsonParse are compiled from the
// downloaded client (generated Lexer identifiers normalized only). This verifies
// codec interoperability, not gameplay.
using System;
using System.Collections.Generic;
using System.Net;
using System.Text;
using LitJson;
using OAuth;

namespace UnityEngine { public class MonoBehaviour {} }
public static class Debug { public static void LogWarning(object s) { Console.Error.WriteLine(s); } }
public class WebAPI { public class SendBaseData {} public class RecvBaseData {} }
namespace WebAPIRequest { public class RequestBase { public string apiId; public WebAPI.SendBaseData param; } }

public class ClientProtocolProbe
{
    static string Send(string url, Dictionary<string, string> form, string token, string secret)
    {
        var oauth = new OAuthRequest { Method = "POST", RequestUrl = url,
            ConsumerKey = Secure.ConstValue.MULTI_LANGUAGE_COUNSUMER_KEY,
            ConsumerSecret = Secure.ConstValue.MULTI_LANGUAGE_COUNSUMER_SECRET,
            Token = token, TokenSecret = secret };
        var header = oauth.GetAuthorizationHeader(form);
        var parts = new List<string>();
        foreach (var item in form) parts.Add(Uri.EscapeDataString(item.Key) + "=" + Uri.EscapeDataString(item.Value));
        using (var client = new WebClient()) {
            client.Encoding = Encoding.UTF8;
            client.Headers[HttpRequestHeader.Authorization] = header;
            client.Headers[HttpRequestHeader.ContentType] = "application/x-www-form-urlencoded";
            return client.UploadString(url, string.Join("&", parts));
        }
    }

    public static int Main(string[] args)
    {
        try {
            string root = args.Length == 0 ? "http://127.0.0.1:3000" : args[0];
            string auth = Send(root + "/appApi/AuthUserByUuid", new Dictionary<string,string>{{"x_uuid", "client-codec-probe"}}, null, null);
            var identity = JsonMapper.ToObject(auth);
            if ((int)identity["venus_status"] != 1) throw new Exception("Auth rejected: " + auth);
            string token = (string)identity["token_key"], secret = (string)identity["token_secret"];
            string response = Send(root + "/app/ActiveController", new Dictionary<string,string> {
                {"activityList", "{\"000001\":{\"uniqueRequestId\":1,\"requestStatus\":0,\"osType\":\"2\"}}"},
                {"appVersion", "2.6.0"}, {"assetVersion", "0"}, {"disabledVC", "1"}
            }, token, secret);
            if (WebAPIJsonParse.GetInt(response, "venus_status") != 1) throw new Exception("Login rejected: " + response);
            var body = JsonMapper.ToObject(WebAPIJsonParse.GetBody(response));
            if ((string)body["playerInfo"]["userId"] != identity["x_user_id"].ToString()) throw new Exception("User id mismatch");
            if (WebAPIJsonParse.GetString(response, "osType") != "2") throw new Exception("Header mismatch");
            string batch = Send(root + "/app/ActiveController", new Dictionary<string,string> {
                {"activityList", "{\"000501\":{\"uniqueRequestId\":2},\"990102\":{\"uniqueRequestId\":3}}"},
                {"appVersion", "2.6.0"}, {"assetVersion", "0"}, {"disabledVC", "1"}
            }, token, secret);
            var requests = new List<WebAPIRequest.RequestBase> {
                new WebAPIRequest.RequestBase { apiId = "000501" }, new WebAPIRequest.RequestBase { apiId = "990102" }
            };
            var responses = WebAPIJsonParse.GetResponseList(requests, batch);
            if (responses.Count != 2) throw new Exception("Batch decode mismatch");
            if (JsonMapper.ToObject(responses["990102"])["monsterM"].Count == 0) throw new Exception("Empty master");
            string emptyResponse = Send(root + "/app/ActiveController", new Dictionary<string,string> {
                {"activityList", "{\"020305\":{\"uniqueRequestId\":4,\"countryCode\":2}}"},
                {"appVersion", "2.6.0"}, {"assetVersion", "0"}, {"disabledVC", "1"}
            }, token, secret);
            if (WebAPIJsonParse.GetInt(emptyResponse, "venus_status") != 1) throw new Exception("Language save rejected");
            if (JsonMapper.ToObject(WebAPIJsonParse.GetBody(emptyResponse)).Count != 0) throw new Exception("Empty response decode mismatch");
            Console.WriteLine("PASS: original client parser accepts empty API response");
            Console.WriteLine("PASS: original client OAuth + LitJson + WebAPIJsonParse auth, login and batch/master decoding");
            return 0;
        } catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
}
