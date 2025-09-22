using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using MCPForUnity.Editor.Helpers;

namespace MCPForUnity.Editor.Tools
{
    /// <summary>
    /// Provides automation commands used by the MCP pipeline (project probing, compilation, tests, builds).
    /// </summary>
    public static class Automation
    {
        public static object HandleUnityProjectProbe(JObject @params)
        {
            try
            {
                string projectPath = ResolveProjectPath(@params);
                var data = new
                {
                    unityVersion = Application.unityVersion,
                    projectPath,
                    apiCompatibilityLevel = PlayerSettings.GetApiCompatibilityLevel(EditorUserBuildSettings.selectedBuildTargetGroup).ToString(),
                    scriptingBackendPerPlatform = BuildScriptingBackendMap(),
                    packages = ReadPackages(projectPath),
                    editorApplicationPath = EditorApplication.applicationPath,
                    editorContentsPath = EditorApplication.applicationContentsPath,
                    roslynCompilerPath = ResolveRoslynCompilerPath(),
                    managedAssemblySearchPaths = ResolveAssemblySearchPaths()
                };

                return Response.Success("Unity project probe completed", data);
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Automation] unity_project_probe failed: {ex.Message}\n{ex.StackTrace}");
                return Response.Error("unity_project_probe_failed", new { message = ex.Message });
            }
        }

        public static object HandleUnityCompile(JObject @params)
        {
            try
            {
                AIBuild.VerifyCompile();
                var data = new
                {
                    logPath = Application.consoleLogPath
                };
                return Response.Success("Unity compilation completed", data);
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Automation] unity_compile failed: {ex.Message}\n{ex.StackTrace}");
                return Response.Error("unity_compile_failed", new { message = ex.Message });
            }
        }

        public static object HandleUnityRunTests(JObject @params)
        {
            try
            {
                string testMode = @params?["platform"]?.ToString();
                if (string.IsNullOrWhiteSpace(testMode))
                {
                    testMode = "EditMode";
                }

                string resultsPath = @params?["resultsPath"]?.ToString();
                if (string.IsNullOrWhiteSpace(resultsPath))
                {
                    resultsPath = Path.Combine("Logs", $"{testMode.ToLowerInvariant()}-results.xml");
                }

                string absoluteResultsPath = AIBuild.RunTests(testMode, resultsPath);
                var data = new
                {
                    resultsPath = absoluteResultsPath,
                    logPath = Application.consoleLogPath,
                    testMode
                };

                return Response.Success("Unity tests executed", data);
            }
            catch (Exception ex)
            {
                Debug.LogError($"[Automation] unity_run_tests failed: {ex.Message}\n{ex.StackTrace}");
                return Response.Error("unity_run_tests_failed", new { message = ex.Message });
            }
        }

        public static object HandleUnityBuildIl2Cpp(JObject @params)
        {
            // Optional capability: return a structured error if no custom build pipeline is provided.
            return Response.Error("unity_build_il2cpp_not_implemented", new
            {
                message = "IL2CPP build automation is not configured for this project. Provide a BuildScript entry point to enable it."
            });
        }

        private static string ResolveProjectPath(JObject @params)
        {
            string projectPath = null;
            if (@params != null)
            {
                projectPath = @params.Value<string>("projectPath");
            }

            if (string.IsNullOrEmpty(projectPath))
            {
                projectPath = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            }
            else
            {
                projectPath = Path.GetFullPath(projectPath);
            }

            return projectPath;
        }

        private static Dictionary<string, string> BuildScriptingBackendMap()
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (BuildTargetGroup group in Enum.GetValues(typeof(BuildTargetGroup)))
            {
                if (group == BuildTargetGroup.Unknown)
                {
                    continue;
                }

                try
                {
                    ScriptingImplementation backend = PlayerSettings.GetScriptingBackend(group);
                    result[group.ToString()] = backend.ToString();
                }
                catch
                {
                    // Ignore groups that are not supported in the current installation.
                }
            }

            return result;
        }

        private static IEnumerable<object> ReadPackages(string projectPath)
        {
            try
            {
                string packagesLock = Path.Combine(projectPath, "Packages", "packages-lock.json");
                if (!File.Exists(packagesLock))
                {
                    return Array.Empty<object>();
                }

                string json = File.ReadAllText(packagesLock);
                var root = JObject.Parse(json);
                var deps = root["dependencies"] as JObject;
                if (deps == null)
                {
                    return Array.Empty<object>();
                }

                return deps.Properties()
                    .Select(p => new
                    {
                        name = p.Name,
                        version = p.Value?["version"]?.ToString() ?? string.Empty
                    })
                    .ToArray();
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[Automation] Failed to read packages-lock.json: {ex.Message}");
                return Array.Empty<object>();
            }
        }

        private static string ResolveRoslynCompilerPath()
        {
            string contents = EditorApplication.applicationContentsPath;
            string candidate = Path.Combine(contents, "Tools", "Roslyn", Application.platform == RuntimePlatform.WindowsEditor ? "csc.exe" : "csc");
            return File.Exists(candidate) ? candidate : string.Empty;
        }

        private static string[] ResolveAssemblySearchPaths()
        {
            string contents = EditorApplication.applicationContentsPath;
            var paths = new List<string>
            {
                Path.Combine(contents, "Managed"),
                Path.Combine(contents, "Managed", "UnityEngine"),
                Path.Combine(contents, "Managed", "UnityEditor"),
                Path.Combine(contents, "Managed", "UnityEngine", "UnityEngine"),
                Path.Combine(contents, "MonoBleedingEdge", "lib", "mono", "4.8.0-api"),
                Path.Combine(contents, "MonoBleedingEdge", "lib", "mono", "unityjit")
            };

            return paths.Where(Directory.Exists).Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        }
    }
}
