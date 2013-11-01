/* Contest Management System
 * Copyright © 2013 Luca Wehrstedt <luca.wehrstedt@gmail.com>
 * Copyright © 2013 William Di Luigi <williamdiluigi@gmail.com>
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 */
'use strict';

/* Signin page */

angular.module('pws.forum', [])
  .controller('ForumsCtrl', function ($scope, $http, userManager,
        notificationHub, navbarManager) {
    navbarManager.setActiveTab(2);
    $http.post('forum', {
        'action': 'list',
        'user':   userManager.getUsername(),
        'token':  userManager.getToken()
      })
      .success(function(data, status, headers, config) {
        $scope.forums = data.forums;
      }).error(function(data, status, headers, config) {
        notificationHub.createAlert('danger', 'Errore interno', 2);
      });
  })
  .controller('ForumCtrl', function ($scope, $http, $stateParams,
        userManager, navbarManager, notificationHub) {
    navbarManager.setActiveTab(0);
    $scope.getTopics = function() {
      $http.post('topic', {
          'action': 'list',
          'username':   userManager.getUsername(),
          'token':  userManager.getToken(),
          'forum':  $stateParams.forumId,
          'first':  0,
          'last':   10000
        })
        .success(function(data, status, headers, config) {
          $scope.topics = data.topics;
          $scope.numTopics = data.num;
          $scope.forumTitle = data.title;
          $scope.forumDesc = data.description;
          for (var i in $scope.topics) {
            $scope.topics[i].date = new Date($scope.topics[i].timestamp * 1000);
            $scope.topics[i].date = $scope.topics[i].date.toLocaleString();
          }
        }).error(function(data, status, headers, config) {
          notificationHub.createAlert('danger', 'Errore interno', 2);
        });
    }
    $scope.newTopic = function() {
      $http.post('topic', {
          'action': 'new',
          'title':  prompt('Titolo:'),
          'text':   prompt('Contenuto:'),
          'username':   userManager.getUsername(),
          'token':  userManager.getToken(),
          'forum':  $stateParams.forumId
        })
        .success(function(data, status, headers, config) {
          notificationHub.createAlert('info', 'Topic creato', 1);
          $scope.getTopics();
          //~ $location.path();
        }).error(function(data, status, headers, config) {
          notificationHub.createAlert('danger', 'Errore interno', 2);
        });
    };
    $scope.getTopics();
  })
  .controller('TopicCtrl', function ($scope, $http, $stateParams,
        userManager, navbarManager, notificationHub) {
    navbarManager.setActiveTab(2);
    $scope.getPosts = function() {
      $http.post('post', {
          'action': 'list',
          'username':   userManager.getUsername(),
          'token':  userManager.getToken(),
          'topic':  $stateParams.topicId,
          'first':  0,
          'last':   10000
        })
        .success(function(data, status, headers, config) {
          $scope.posts = data.posts;
          $scope.numPosts = data.num;
          $scope.title = data.title;
          $scope.forumTitle = data.forumTitle;
          for (var i in $scope.posts) {
            $scope.posts[i].date = new Date($scope.posts[i].timestamp * 1000);
            $scope.posts[i].date = $scope.posts[i].date.toLocaleString();
          }
        }).error(function(data, status, headers, config) {
          notificationHub.createAlert('danger', 'Errore interno', 2);
        });
    }
    $scope.newPost = function() {
      $http.post('post', {
          'action': 'new',
          'text':   $scope.newText,
          'username':   userManager.getUsername(),
          'token':  userManager.getToken(),
          'topic':  $stateParams.topicId
        })
        .success(function(data, status, headers, config) {
          notificationHub.createAlert('info', 'Risposta inviata', 1);
          $scope.getPosts();
          //~ $location.path();
        }).error(function(data, status, headers, config) {
          notificationHub.createAlert('danger', 'Errore interno', 2);
        });
    };
    $scope.getPosts();
  });