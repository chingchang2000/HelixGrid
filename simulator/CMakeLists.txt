cmake_minimum_required(VERSION 3.20)
project(helixgrid_simulator VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(helix-sim src/main.cpp)

if(MSVC)
  target_compile_options(helix-sim PRIVATE /W4 /permissive-)
else()
  target_compile_options(helix-sim PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
endif()

include(CTest)
if(BUILD_TESTING)
  add_test(NAME smoke COMMAND helix-sim --demo --workers 4 --runs 10 --seed 42)
endif()
