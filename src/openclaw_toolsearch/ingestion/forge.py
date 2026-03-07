from typing import Dict, List, Any, Optional
import re

class ForgeEngine:
    """
    Clean-room implementation of an OpenAPI-to-MCP tool definition converter.
    """
    
    @staticmethod
    def forge_tools(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        tools = []
        paths = spec.get("paths", {})
        servers = spec.get("servers", [])
        base_url = servers[0].get("url", "") if servers else ""

        for path, methods in paths.items():
            for method, operation in methods.items():
                if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                    continue

                tool_name = ForgeEngine._generate_name(method, path, operation)
                description = operation.get("description") or operation.get("summary") or f"{method.upper()} {path}"
                
                input_schema = ForgeEngine._map_parameters(operation, spec)
                
                # Metadata for execution routing later
                annotations = {
                    "source": "openapi",
                    "method": method.upper(),
                    "path": path,
                    "base_url": base_url
                }

                tools.append({
                    "name": tool_name,
                    "description": description,
                    "inputSchema": input_schema,
                    "annotations": annotations
                })
        return tools

    @staticmethod
    def _generate_name(method: str, path: str, operation: Any) -> str:
        op_id = operation.get("operationId")
        if op_id:
            return ForgeEngine._safe_name(op_id)
        
        # Fallback to method_path
        clean_path = re.sub(r'[^a-zA-Z0-9]', '_', path).strip('_')
        return f"{method.lower()}_{clean_path}"

    @staticmethod
    def _safe_name(name: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]', '_', name)

    @staticmethod
    def _map_parameters(operation: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
        properties = {}
        required = []

        # 1. Path/Query/Header Parameters
        for param in operation.get("parameters", []):
            # Handle refs if needed (simplified for V1)
            name = param.get("name")
            p_type = param.get("schema", {}).get("type", "string")
            p_desc = param.get("description", "")
            
            properties[name] = {
                "type": p_type,
                "description": f"[{param.get('in')}] {p_desc}"
            }
            if param.get("required"):
                required.append(name)

        # 2. Request Body (JSON)
        body = operation.get("requestBody", {})
        content = body.get("content", {})
        json_content = content.get("application/json", {})
        schema = json_content.get("schema", {})
        
        if schema:
            # If it's a direct object, merge its properties
            if schema.get("type") == "object":
                for k, v in schema.get("properties", {}).items():
                    properties[k] = v
                required.extend(schema.get("required", []))
            else:
                properties["requestBody"] = schema

        return {
            "type": "object",
            "properties": properties,
            "required": list(set(required))
        }
